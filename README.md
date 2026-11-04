# Assistente Spesa - HA

<img src="custom_components/lista_spesa/brand/icon.png" alt="Icona di Assistente Spesa" width="96">

Integrazione per Home Assistant: la lista della spesa condivisa di casa, con ruoli Adulto e Junior, divisione dei costi e notifiche.
Serve all'app **Assistente Spesa** per Android e iOS, che per ora non è pubblica.

*Home Assistant integration for the household shopping list used by the Assistente Spesa mobile app. Interface available in Italian and English.*

## Cosa aggiunge a Home Assistant

- La lista condivisa, salvata in Home Assistant e inclusa nei backup.
- I comandi WebSocket usati dall'app, che filtrano i dati in base all'utente.
- Un'entità todo, visibile nelle dashboard e usabile con Assist. Non mostra i prodotti "Solo adulti".
- Un sensore con il numero di prodotti da comprare, utile nelle automazioni.
- Facoltativi: notifiche push con Firebase e registrazione delle spese in Firefly III.

## Installazione con HACS

1. In HACS apri il menu ⋮ → **Repository personalizzati**.
2. Incolla `https://github.com/118computer/AssistenteSpesaHA`, scegli la categoria **Integrazione** e conferma.
3. Cerca **Assistente Spesa - HA** e premi **Scarica**.
4. Riavvia Home Assistant.
5. Vai in **Impostazioni → Dispositivi e servizi → Aggiungi integrazione** e cerca **Assistente Spesa**.

## Ruoli

In **Impostazioni → Dispositivi e servizi → Assistente Spesa → Configura** scegli il ruolo di ogni utente.

| Ruolo | Aggiunge e insiste | Fa la spesa | Vede i costi | Tasti nascosti |
|---|---|---|---|---|
| **Adulto** (predefinito) | sì | sì | sì | sì |
| **Junior** | sì | no | no | no |
| **Nessuno** | non può entrare nell'app | | | |

## Aggiornamenti

Le nuove versioni compaiono in HACS come aggiornamento disponibile. Questo repository viene aggiornato in automatico a ogni versione.
