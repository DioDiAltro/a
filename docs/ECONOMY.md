# L'economia delle biglie

Questo documento è la specifica normativa dell'economia. Se il codice e questo
file divergono, è il codice a essere sbagliato.

## Il problema

Ogni meccanica divertente di questa piattaforma è una **fonte** di biglie:
lotterie che pagano, RTP passivo che piove, riscatti che raddoppiano, duelli che
redistribuiscono. Nessuna di queste, da sola, è un drenaggio.

Un'economia con sole fonti si gonfia. Le biglie perdono valore, i venditori
alzano i prezzi, i nuovi utenti trovano un mercato in cui il loro deposito
compra sempre meno, e la piattaforma muore — non per un bug, ma per aritmetica.

## La regola non negoziabile

> **Solo un deposito confermato in euro conia biglie.**

Non le vincite, non i duelli, non il drip, non le promozioni. Il conto di
sistema `ISSUANCE` è l'unico punto di emissione, e il suo saldo negativo è —
per costruzione — esattamente la quantità di biglie in circolazione, cioè il
nostro debito verso gli utenti.

Le vincite si pagano dal **bankroll della casa** (`HOUSE_REVENUE`), che accumula
i margini. In una giornata fortunata per i giocatori il bankroll scende, anche
sotto zero; rientra per aritmetica, perché ogni gioco ha RTP < 100%. Questo è
scomodo da guardare su un grafico e corretto da un punto di vista contabile:
il jackpot di oggi sono le perdite di ieri che cambiano proprietario, non
moneta nuova.

## Le fonti (tutte tracciate)

| Fonte | Conia? | Da dove viene |
|---|---|---|
| Deposito in € | **sì** | `ISSUANCE` |
| Vincita minigame | no | `HOUSE_REVENUE` |
| Riscatto rischioso vinto | no | `HOUSE_REVENUE` |
| Premio lotteria | no | biglietti già in `ESCROW` |
| Vittoria duello | no | la posta dell'avversario |
| Regalo tra utenti | no | ritira PIENA, emette DONO in pari |
| **Drip passivo (es. 2)** | **no** | `REDISTRIBUTION_POOL` |

Il drip è il caso interessante. La richiesta era: *"un utente che non sta
facendo nulla può comunque guadagnare letteralmente a caso qualche biglia"*.
Preso alla lettera sarebbe emissione pura e il modo più veloce per distruggere
la valuta. Implementato come **redistribuzione**, la sensazione per l'utente è
identica — piovono biglie senza fare niente — ma ogni biglia che piove è stata
prima guadagnata dalla piattaforma. Se il pool è vuoto, non piove. Una settimana
tranquilla dà un drip sottile, ed è giusto così.

## I drenaggi

| Drenaggio | Misura | Dove finisce |
|---|---|---|
| Commissione marketplace | 5% | `HOUSE_REVENUE` |
| Fee riscatto sicuro | 1% | `HOUSE_REVENUE` |
| Margine sui minigame | 6% (RTP 94%) | `HOUSE_REVENUE` |
| Rake sui duelli | 4% | `HOUSE_REVENUE` |
| Crediti non riscattati | 100% dopo 30gg | `REDISTRIBUTION_POOL` |
| Beneficenza | 3% dei ricavi | `CHARITY_POOL` |

## Il 3% in beneficenza

La richiesta era il 3% *"di tutto il denaro che gira nel sito"*. Applicato al
volume transato letteralmente, il 3% supererebbe il margine sulla maggior parte
delle transazioni: su una vendita da 100 biglie incassiamo 5 biglie di
commissione e ne verseremmo 3 di beneficenza, lasciando 2 biglie per coprire
processing, hosting e frodi. Sulle vendite a margine più sottile la piattaforma
pagherebbe per operare, e una piattaforma che chiude versa zero.

Quindi il 3% è calcolato **sui ricavi della casa**, non sul volume. Viene
prelevato in modo sincrono dentro la stessa transazione che riconosce il ricavo
(`wallet.services.recognise_revenue`), quindi il numero sulla pagina pubblica di
trasparenza è sempre un saldo reale del libro mastro, mai una stima. Se preferisci
la versione letterale il parametro è uno solo — `CHARITY_BASIS_POINTS` — ma va
alzata la commissione per compensare, e va detto: sarebbe una scelta di
marketing pagata dai venditori.

## I quattro tipi di biglia

Non sono varietà estetiche: portano **diritti diversi**, e tenerli come asset
separati significa che è il libro mastro a farli rispettare, non la memoria di
chi scrive il prossimo endpoint.

| | Origine | Spendibile | Puntabile | Prelevabile | Regalabile |
|---|---|---|---|---|---|
| **PIENA** | deposito in € | ✓ | ✓ | ✓ | ✓ |
| **VINTA** | gioco, lotterie, drip | ✓ | ✓ | ✗ | ✗ |
| **DONO** | regalo ricevuto | ✓ | ✓ | ✗ | ✓ |

La linea che conta è **PIENA prelevabile / VINTA no**. Senza di essa, il
percorso "deposito 1000 € → gioco una volta → prelevo come vincita" trasforma la
piattaforma in un servizio di trasferimento denaro, che è esattamente la forma
di un canale di riciclaggio. Il cambio di asset nel regalo (spendi PIENA,
l'altro riceve DONO) chiude la stessa falla tra utenti.

La **scaglia** è l'unità minore, 1/100 di biglia — non un quarto tipo. Serve a
far sentire il drip frequente senza gonfiare nulla: la dashboard mostra
"47/100 verso la prossima biglia" e la fusione è un momento di gioco, ma il
libro mastro vede solo interi.

## L'ordine di spesa

Si spende prima VINTA, poi DONO, poi PIENA (`core.money.SPEND_PRIORITY`).
Consumare per ultimo l'asset prelevabile è più corretto verso l'utente — gli
lascia intatto il diritto di riprendersi i soldi il più a lungo possibile — e
riduce la nostra passività di rimborso. Le due cose coincidono, il che è di
solito il segno che la regola è giusta.

## Sorveglianza

`economy.tasks.audit_realised_rtp` confronta ogni sei ore l'RTP realizzato per
gioco con quello configurato. Uno scostamento significa che una tabella dei
premi è sbagliata, e va trattato come un incidente: le tabelle sono la
promessa fatta al giocatore.

`wallet.ledger.reconcile()` dimostra che i saldi materializzati coincidono con
la somma delle scritture. Gira in CI e a orario. Un risultato non vuoto
significa che il libro mastro sta mentendo, e la piattaforma deve smettere di
accettare denaro finché non ha guardato una persona.
