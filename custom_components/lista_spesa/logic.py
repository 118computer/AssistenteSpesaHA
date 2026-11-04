"""Logica pura di Assistente Spesa (nessun import di Home Assistant: testabile con unittest)."""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any, Iterable


def normalize_key(name: str) -> str:
    """Minuscolo, senza accenti, spazi singoli."""
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.strip().lower())


def similarity(a: str, b: str) -> float:
    """0..1: quanto due nomi di prodotto si somigliano."""
    ka, kb = normalize_key(a), normalize_key(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    return difflib.SequenceMatcher(None, ka, kb).ratio()


def find_similar(name: str, existing: Iterable[dict[str, Any]], threshold: float = 0.8) -> dict[str, Any] | None:
    """Prodotto simile già in lista (uguale, o una parola contenuta, o quasi uguale)."""
    key = normalize_key(name)
    best: tuple[float, dict[str, Any]] | None = None
    for item in existing:
        other = normalize_key(item.get("name", ""))
        if not other:
            continue
        if other == key:
            return item
        score = similarity(key, other)
        words = set(other.split())
        if key in words or other in set(key.split()):
            score = max(score, 0.85)
        if score >= threshold and (best is None or score > best[0]):
            best = (score, item)
    return best[1] if best else None


def first_name(full_name: str) -> str:
    parts = (full_name or "").strip().split()
    return parts[0] if parts else ""


def display_names(users: Iterable[dict[str, Any]]) -> dict[str, str]:
    """id → nome da mostrare: primo nome, o nome completo se due utenti hanno lo stesso primo nome."""
    users = list(users)
    counts: dict[str, int] = {}
    for user in users:
        fn = normalize_key(first_name(user.get("full_name", "")))
        counts[fn] = counts.get(fn, 0) + 1
    result: dict[str, str] = {}
    for user in users:
        full = (user.get("full_name") or "").strip()
        fn = first_name(full)
        result[user["id"]] = full if counts.get(normalize_key(fn), 0) > 1 else (fn or full)
    return result


def charged_to(line: dict[str, Any], junior_to_adult: dict[str, str], adult_ids: list[str]) -> str | None:
    """A chi va il costo di una riga: None = parte comune."""
    if line.get("price_cents") is None or line.get("for_whom") != "me":
        return None
    requester = line.get("requested_by")
    if requester in adult_ids:
        return requester
    adult = junior_to_adult.get(requester or "")
    if adult in adult_ids:
        return adult
    return None


def split_costs(
    lines: list[dict[str, Any]],
    receipt_total_cents: int | None,
    payer_id: str,
    adult_ids: list[str],
    junior_to_adult: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Regola di divisione (docs/PROTOCOLLO.md). Restituisce totale, charged_to per riga e quote."""
    junior_to_adult = junior_to_adult or {}
    adults = sorted(set(adult_ids))
    marked = sum(int(l["price_cents"]) for l in lines if l.get("price_cents") is not None)
    total = int(receipt_total_cents) if receipt_total_cents is not None else marked
    charged = [charged_to(l, junior_to_adult, adults) for l in lines]
    own: dict[str, int] = {a: 0 for a in adults}
    own_items: dict[str, list[dict[str, Any]]] = {a: [] for a in adults}
    for line, who in zip(lines, charged):
        if who is not None:
            own[who] += int(line["price_cents"])
            own_items[who].append({"name": line.get("name", ""), "price_cents": int(line["price_cents"])})
    common = total - sum(own.values())
    shares: list[dict[str, Any]] = []
    if adults:
        n = len(adults)
        base = common // n
        rest = common - base * n
        receiver = payer_id if payer_id in adults else adults[0]
        for a in adults:
            c = base + (rest if a == receiver else 0)
            shares.append(
                {
                    "user_id": a,
                    "common_cents": c,
                    "own_cents": own[a],
                    "total_cents": c + own[a],
                    "own_items": own_items[a],
                }
            )
    return {"total_cents": total, "marked_total_cents": marked, "charged_to": charged, "shares": shares}


def update_aisle(aisle: dict[str, float], ordered_keys: list[str], weight: float = 0.5) -> dict[str, float]:
    """Media mobile della posizione di spunta (0..1) di ogni prodotto in quel supermercato."""
    result = dict(aisle)
    n = len(ordered_keys)
    if n == 0:
        return result
    for idx, key in enumerate(ordered_keys):
        pos = idx / (n - 1) if n > 1 else 0.5
        old = result.get(key)
        result[key] = round(pos if old is None else old * (1 - weight) + pos * weight, 4)
    return result


def compute_balances(
    expenses: list[dict[str, Any]], settlements: list[dict[str, Any]], user_ids: Iterable[str]
) -> dict[str, Any]:
    """Saldi netti (> 0 deve ricevere) e trasferimenti minimi."""
    net: dict[str, int] = {u: 0 for u in user_ids}
    for exp in expenses:
        payer = exp.get("payer_id")
        shares = exp.get("shares") or []
        if not payer or not shares:
            continue
        paid = sum(int(s.get("total_cents", 0)) for s in shares)
        net[payer] = net.get(payer, 0) + paid
        for s in shares:
            uid = s.get("user_id")
            net[uid] = net.get(uid, 0) - int(s.get("total_cents", 0))
    for st in settlements:
        net[st["from_id"]] = net.get(st["from_id"], 0) + int(st["amount_cents"])
        net[st["to_id"]] = net.get(st["to_id"], 0) - int(st["amount_cents"])
    creditors = sorted(([u, v] for u, v in net.items() if v > 0), key=lambda x: (-x[1], x[0]))
    debtors = sorted(([u, -v] for u, v in net.items() if v < 0), key=lambda x: (-x[1], x[0]))
    transfers: list[dict[str, Any]] = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        amount = min(debtors[i][1], creditors[j][1])
        if amount > 0:
            transfers.append({"from_id": debtors[i][0], "to_id": creditors[j][0], "amount_cents": amount})
        debtors[i][1] -= amount
        creditors[j][1] -= amount
        if debtors[i][1] == 0:
            i += 1
        if creditors[j][1] == 0:
            j += 1
    return {
        "balances": [{"user_id": u, "net_cents": v} for u, v in sorted(net.items())],
        "transfers": transfers,
    }


def receipt_text_key(text: str) -> str:
    """Chiave di una riga di scontrino: senza numeri e prezzi, normalizzata."""
    key = normalize_key(text)
    key = re.sub(r"\d+[.,]?\d*", " ", key)
    key = re.sub(r"[^a-z ]", " ", key)
    return re.sub(r"\s+", " ", key).strip()


def _abbrev_score(receipt_text: str, item_name: str) -> float:
    rt = [t for t in receipt_text_key(receipt_text).split() if len(t) >= 3]
    it = [t for t in normalize_key(item_name).split() if len(t) >= 3]
    if not rt or not it:
        return 0.0
    hits = 0
    for word in it:
        prefix = word[: min(len(word), 4)]
        if any(r.startswith(prefix) or word.startswith(r) for r in rt):
            hits += 1
    token_score = hits / len(it)
    ratio = difflib.SequenceMatcher(None, receipt_text_key(receipt_text), normalize_key(item_name)).ratio()
    return max(token_score, ratio)


def match_receipt(
    lines: list[dict[str, Any]],
    expense_lines: list[dict[str, Any]],
    store_map: dict[str, str],
    threshold: float = 0.6,
) -> list[dict[str, Any]]:
    """Abbina le righe lette dallo scontrino ai prodotti presi.

    store_map: chiave riga scontrino → key prodotto (abbinamenti ricordati per quel supermercato).
    """
    by_key = {l.get("key"): l for l in expense_lines}
    result: list[dict[str, Any]] = []
    for line in lines:
        text = line.get("text", "")
        price = int(line.get("price_cents", 0))
        entry: dict[str, Any] = {"text": text, "price_cents": price, "kind": "item", "item_id": None, "match": "none"}
        if price < 0:
            entry["kind"] = "discount"
            result.append(entry)
            continue
        remembered = store_map.get(receipt_text_key(text))
        if remembered and remembered in by_key:
            entry["item_id"] = by_key[remembered].get("item_id")
            entry["match"] = "remembered"
            result.append(entry)
            continue
        best: tuple[float, dict[str, Any]] | None = None
        for exp_line in expense_lines:
            score = _abbrev_score(text, exp_line.get("name", ""))
            if score >= threshold and (best is None or score > best[0]):
                best = (score, exp_line)
        if best:
            entry["item_id"] = best[1].get("item_id")
            entry["match"] = "suggested"
        else:
            entry["kind"] = "extra"
        result.append(entry)
    return result


def receipt_prices(lines: list[dict[str, Any]]) -> dict[str, int]:
    """item_id → somma dei prezzi delle righe abbinate."""
    prices: dict[str, int] = {}
    for line in lines:
        if line.get("kind") == "item" and line.get("item_id"):
            prices[line["item_id"]] = prices.get(line["item_id"], 0) + int(line.get("price_cents", 0))
    return prices


def end_counts(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Conteggi del riepilogo di fine spesa sugli elementi della spesa e quelli rimasti da comprare."""
    taken = [i for i in items if i.get("status") == "taken"]
    not_found = [i for i in items if i.get("status") == "not_found"]
    still_open = [i for i in items if i.get("status") == "open"]
    return {
        "taken_count": len(taken),
        "requested_count": len(taken) + len(not_found) + len(still_open),
        "not_found": [i.get("name", "") for i in not_found],
    }
