# New Livingstone Pub · Mo.Ma. Club

Sito del pub in stile scozzese di via Vittorio Aquilino 6, Troia (FG).
Statico: HTML, CSS e JavaScript senza dipendenze. Si apre `index.html` nel browser
oppure si carica la cartella così com'è su qualunque hosting statico.

```
python3 -m http.server 8000   # dalla cartella new-livingstone, poi http://localhost:8000
```

## Cosa si aggiorna e dove

| Cosa | Dove |
|---|---|
| Orari | tabella `#orari` in `index.html`, attributi `data-apre` e `data-chiude`. Lo stato «Aperto ora» e il controllo dei giorni chiusi nel modulo li leggono da lì. Il testo degli orari compare anche nell'hero e nel piè di pagina. |
| Telefono e WhatsApp | costante `WHATSAPP` in `js/main.js` e i link `tel:` in `index.html` |
| Instagram | link a `@newlivingstonepub` in `index.html` |
| Colori | variabili in cima a `css/style.css`. Le coppie testo/sfondo sono verificate AA. |

## Da confermare con il pub

- **Orari**: il sito ufficiale dice da martedì a domenica 19:00–01:00, il profilo
  Instagram dice «chiuso il martedì». Nel sito c'è la prima versione (lunedì chiuso).
- **Super Panino alla Zozzona**: la descrizione dice carne alla brace, pane caldo e
  colata di cheddar. Gli ingredienti esatti vanno verificati.
- **WhatsApp**: il modulo prepara il messaggio per il 328 899 4698. Va verificato che
  quel numero usi WhatsApp.
- **Sottobicchieri**: Leffe, Birra Messina e Bulldog sono disegni originali con il
  nome della birra, non i loghi ufficiali. Per usare i loghi servono i file forniti
  dal distributore o dal marchio.

## Note

- Nessuna foto: le illustrazioni sono SVG disegnati a mano dentro `index.html`.
- I font (Ultra, Young Serif, Libre Franklin, Caveat, licenza OFL) sono in `fonts/`:
  il sito non fa richieste a Google né ad altri servizi esterni.
- Con «riduci movimento» attivo nel sistema le animazioni sono spente e si vede subito
  lo stato finale.
