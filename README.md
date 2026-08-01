# biglie

Un marketplace dove si compra e si vende qualsiasi cosa — oggetti fisici,
prodotti digitali, scommesse — e dove la valuta è la **biglia**. Non è un
e-commerce con sopra un livello di gamification: le meccaniche di gioco *sono*
il modo in cui si vende e si compra.

---

## Il modello mentale in sessanta secondi

**Ogni biglia che entra nel sistema passa da un riscatto.** Vincita, regalo,
incasso di una vendita, rimborso, pioggia casuale: niente accredita mai
direttamente il saldo spendibile. Tutto atterra come *credito in attesa* nella
bacheca, e l'utente sceglie:

- **prendile e basta** → arrivano, meno l'1%
- **rischiale** → decide un minigame: slot, ruota, scala, testa o croce

Non è una funzione fra le altre — è il collo di bottiglia dell'intera economia.
Un solo punto di ingresso significa un solo posto dove applicare i limiti, un
solo imbuto da misurare, una sola politica di scadenza, e ogni singola biglia
che entra diventa un'occasione per far provare qualcosa a chi la riceve.

**Solo un deposito confermato in euro conia biglie.** Le vincite si pagano dal
bankroll della casa, non dal nulla. È la regola che permette di essere generosi
con la gamification senza gonfiare la valuta. Il perché sta in
[`docs/ECONOMY.md`](docs/ECONOMY.md), che è la specifica normativa
dell'economia: se il codice e quel documento divergono, è il codice a essere
sbagliato.

---

## Architettura

Un progetto Django ASGI, **un'app per bounded context**, comunicazione
esclusivamente via event bus interno.

```
core/          Amount/Asset, event bus, idempotenza, contratti fra contesti
wallet/        Il libro mastro in partita doppia. Non importa nessuno.
identity/      Utenti, livelli, profilo venditore
compliance/    KYC, limiti, autoesclusione, giornale AML a catena di hash
marketplace/   Annunci (fisici/digitali/scommesse), ordini, escrow, dispute
salemodes/     Registry a plugin: prezzo fisso, asta, lotteria, groupbuy, …
games/         RNG provably fair, minigame, motore RTP, duelli fra venditori
payments/      Depositi e prelievi in euro (Stripe), gate KYC
charity/       Fondo, beneficiari, pagina pubblica di trasparenza
economy/       Pool di redistribuzione, drip passivo, sorveglianza RTP
realtime/      Consumer WebSocket
web/           Design system, guscio, imbuto dei riscatti
```

### Perché non microservizi (ancora)

Il cuore del sistema è transazionale: *«prendi 30 biglie da A, mettile in
escrow, estrai, consegna l'oggetto, paga il venditore, versa il 3% in
beneficenza»*. In un processo è una transazione di database. Spezzata in sei
servizi diventa una saga distribuita con compensazioni, e ogni bug nella saga è
denaro che sparisce o si duplica. È il posto peggiore dove pagare la tassa dei
microservizi.

Quello che serve davvero — poter estrarre un contesto quando servirà — si ottiene
con confini *duri*, e i confini sono verificati meccanicamente:

```bash
pytest tests/test_boundaries.py
```

Quel test fallisce la build se un contesto importa i modelli di un altro. Il
grafo delle dipendenze consentite è scritto lì dentro in forma leggibile: se una
freccia sorprende, è il progetto a essere sbagliato, non il test. Quando un
contesto dovrà diventare un servizio, `core.events.publish` è il punto dove si
innesta un broker e nessuna logica di dominio cambia.

`wallet` non importa nessuno. Il giorno in cui il libro mastro importa il
marketplace è il giorno in cui un bug negli annunci può corrompere un saldo.

---

## Avvio

```bash
cp .env.example .env
docker compose up --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
```

Su http://localhost:8000.

Senza Docker:

```bash
pip install -r requirements-dev.txt
python manage.py migrate
python manage.py runserver
pytest
```

I test girano su SQLite in memoria e non richiedono container: l'invariante di
somma zero è applicata in `wallet.ledger.post`, non da un trigger, proprio
perché regga su entrambi i backend e sia testabile a unità.

---

## `LIVE_MODE`

La piattaforma muove denaro reale e liquida giochi d'azzardo. `LIVE_MODE` è
l'unico interruttore fra «software giocabile» e «operazione regolata». Vale
`False` di default, e `config/checks.py` **si rifiuta di avviare** un deployment
live a cui manchi un controllo di conformità (chiavi Stripe, numero di
concessione, provider KYC) o che abbia `DEBUG` acceso.

Detto una volta e senza giri: il codice è pronto per la produzione, ma esercire
lotterie e giochi con vincite in denaro in Italia richiede una concessione ADM.
Il gate esiste perché nessuno ci finisca dentro per distrazione.

---

## Le cose che è facile sbagliare, e come sono state gestite

| Rischio | Come è affrontato |
|---|---|
| Inflazione della valuta | Solo i depositi coniano. Le vincite escono dal bankroll. Il drip redistribuisce, non emette. |
| Riciclaggio | Solo le biglie PIENE sono prelevabili. I regali cambiano asset in DONO, non prelevabile. |
| RNG truccabile (o accusabile) | Commit-reveal con seme del cliente, campionamento con rifiuto (niente bias da modulo), verifica pubblica. |
| Lotteria estratta al momento comodo | Il venditore non può comprare i propri biglietti; l'estrazione anticipata richiede un preavviso pubblico. |
| Doppio accredito | Chiave di idempotenza su ogni operazione monetaria, più lock di riga sui riscatti. |
| Saldi che divergono dalla realtà | `wallet.ledger.reconcile()` dimostra che i saldi materializzati coincidono con le scritture. Gira in CI. |
| Giornale di audit manomesso | Ogni riga incatena lo SHA-256 della precedente. |
| Duelli come molestia | Opt-in, con tetto per livello e limite di sfide ripetute. |
| Dark pattern sui riscatti | «Prendile» e «rischiale» hanno lo stesso peso visivo, per scelta esplicita di design. |

---

## Test

```bash
pytest                          # tutto
pytest wallet                   # invarianti contabili
pytest compliance               # limiti, autoesclusione, KYC, catena di audit
pytest tests/test_smoke.py      # le pagine rendono davvero
pytest tests/test_boundaries.py # i confini architetturali
```

---

## Stato

Questa è una build in corso. Cosa regge oggi e cosa no, senza abbellimenti.

**280 test verdi, `ruff` pulito, nessuna migrazione mancante.**

| Area | Stato |
|---|---|
| `core` — Amount/Asset, event bus, idempotenza, contratti | ✅ |
| `wallet` — partita doppia, 3 asset, crediti in attesa, hold, riconciliazione | ✅ |
| `identity` — registrazione con verifica 18+, login, profili, livelli | ✅ codice, ⬜ test |
| `compliance` — KYC a soglie, limiti con cooling-off, autoesclusione, giornale AML | ✅ |
| `games` — RNG commit-reveal, 5 minigame, motore RTP, duelli | ✅ |
| `payments` — Stripe, webhook idempotenti, prelievi con gate KYC | ✅ |
| `charity` — beneficiari, distribuzione, trasparenza pubblica | ✅ |
| `economy` — drip passivo, sorveglianza RTP, monitoraggio | ✅ |
| `marketplace` — annunci, ordini, escrow merce | ✅ codice, ⬜ test |
| `salemodes` — registry a plugin, lotteria, asta, prezzo fisso | 🚧 mancano `urls`/`views` e i test |
| `web` — design system, imbuto dei riscatti, superfici canvas | ✅ |
| Infrastruttura — Docker, Celery, Channels, CI, gate `LIVE_MODE` | ✅ |

`tests/test_smoke.py` verifica che le pagine rendano davvero: una suite verde
con la home che dà 500 è peggio di nessuna suite, perché rassicura.

**Cosa manca ancora**

- Test per `salemodes`, `marketplace` e `identity` — incluso quello che conta di
  più: registrare un modo di vendita fittizio dentro il test e portarlo a
  termine **senza toccare il motore ordini**. È l'unica prova che il giunto a
  plugin sia reale e non velleitario.
- `salemodes` non ha ancora viste e URL proprie.
- Lo scenario end-to-end dell'esempio 1 (lotteria con minimo, estrazione
  anticipata, pagamento del venditore) con asserzione esatta sul libro mastro.
- Il CTA "guarda il mercato" nella home punta temporaneamente a `web:fairness`;
  c'è un commento nel punto esatto in cui va ripristinato ora che
  `marketplace:listing_list` esiste.
