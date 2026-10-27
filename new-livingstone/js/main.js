// New Livingstone Pub: animazioni allo scroll, stato "aperto ora" e messaggio di prenotazione.
// Nessuna dipendenza. Gli orari si leggono dalla tabella #orari: si cambiano solo lì.
(() => {
  'use strict';

  const WHATSAPP = '393288994698';
  const GIORNI = ['domenica', 'lunedì', 'martedì', 'mercoledì', 'giovedì', 'venerdì', 'sabato'];

  document.querySelectorAll('[data-anno]').forEach((el) => {
    el.textContent = String(new Date().getFullYear());
  });

  /* ---------- Animazioni: partono quando la sezione entra in vista, una volta sola ---------- */

  const daAnimare = document.querySelectorAll('[data-anim]');
  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver((voci) => {
      for (const v of voci) {
        if (!v.isIntersecting) continue;
        v.target.classList.add('is-in');
        io.unobserve(v.target);
      }
    }, { threshold: 0.35 });
    daAnimare.forEach((el) => io.observe(el));

    // I loop (le braci) girano solo mentre la sezione è visibile
    const loop = new IntersectionObserver((voci) => {
      for (const v of voci) v.target.classList.toggle('in-vista', v.isIntersecting);
    });
    document.querySelectorAll('[data-loop]').forEach((el) => loop.observe(el));
  } else {
    daAnimare.forEach((el) => el.classList.add('is-in'));
  }

  /* ---------- Orari e stato "aperto ora" (ora di Troia, non del telefono) ---------- */

  const minuti = (hhmm) => {
    const [h, m] = hhmm.split(':').map(Number);
    return h * 60 + m;
  };

  const righe = [...document.querySelectorAll('#orari tr[data-giorno]')];
  const orari = new Map(righe.map((tr) => [
    Number(tr.dataset.giorno),
    tr.dataset.apre
      ? { apre: minuti(tr.dataset.apre), chiude: minuti(tr.dataset.chiude), testoApre: tr.dataset.apre, testoChiude: tr.dataset.chiude }
      : null,
  ]));

  function adessoATroia() {
    const parti = new Intl.DateTimeFormat('en-GB', {
      timeZone: 'Europe/Rome', year: 'numeric', month: '2-digit', day: '2-digit',
      weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }).formatToParts(new Date());
    const val = (tipo) => parti.find((p) => p.type === tipo)?.value;
    const giorni = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
    return {
      giorno: giorni[val('weekday')],
      min: Number(val('hour')) * 60 + Number(val('minute')),
      iso: `${val('year')}-${val('month')}-${val('day')}`,
    };
  }

  // Chiusura "01:00" dopo un'apertura "19:00" vuol dire l'una di notte del giorno dopo
  const passaMezzanotte = (o) => o.chiude <= o.apre;

  function statoDelPub() {
    const { giorno, min } = adessoATroia();
    const oggi = orari.get(giorno);
    const ieri = orari.get((giorno + 6) % 7);

    if (ieri && passaMezzanotte(ieri) && min < ieri.chiude) {
      return { aperto: true, testo: `Aperto ora · chiude alle ${ieri.testoChiude}`, serata: (giorno + 6) % 7 };
    }
    if (oggi && min >= oggi.apre && (passaMezzanotte(oggi) || min < oggi.chiude)) {
      return { aperto: true, testo: `Aperto ora · chiude alle ${oggi.testoChiude}`, serata: giorno };
    }
    if (oggi && min < oggi.apre) {
      return { aperto: false, testo: `Chiuso ora · apre oggi alle ${oggi.testoApre}`, serata: giorno };
    }
    for (let i = 1; i <= 7; i++) {
      const g = (giorno + i) % 7;
      const o = orari.get(g);
      if (o) return { aperto: false, testo: `Chiuso ora · apre ${i === 1 ? 'domani' : GIORNI[g]} alle ${o.testoApre}`, serata: giorno };
    }
    return null;
  }

  if (righe.length) {
    const stato = statoDelPub();
    if (stato) {
      document.querySelectorAll('[data-stato]').forEach((el) => {
        el.textContent = stato.testo;
        el.dataset.aperto = stato.aperto ? 'si' : 'no';
      });
      righe.find((tr) => Number(tr.dataset.giorno) === stato.serata)?.classList.add('oggi');
    }
  }

  /* ---------- Prenotazione: prepara il messaggio, non lo invia da sola ---------- */

  const modulo = document.getElementById('modulo-prenota');
  if (!modulo) return;

  const campoNome = modulo.elements.nome;
  const campoGiorno = modulo.elements.giorno;
  const errore = document.getElementById('p-errore');
  const riepilogo = document.getElementById('p-riepilogo');
  const testoMsg = document.getElementById('p-testo');
  const linkWhatsapp = document.getElementById('p-whatsapp');

  const giornoDellaSettimana = (iso) => new Date(`${iso}T12:00:00`).getDay();
  const aggiungiGiorni = (iso, n) => {
    const d = new Date(`${iso}T12:00:00`);
    d.setDate(d.getDate() + n);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  };

  // Il giorno proposto è la prossima sera di apertura
  const oggiISO = adessoATroia().iso;
  campoGiorno.min = oggiISO;
  if (!campoGiorno.value) {
    let proposta = oggiISO;
    for (let i = 0; i < 7 && !orari.get(giornoDellaSettimana(proposta)); i++) proposta = aggiungiGiorni(oggiISO, i + 1);
    campoGiorno.value = proposta;
  }

  function mostraErrore(messaggio, campo) {
    errore.textContent = messaggio;
    errore.hidden = false;
    campo.setAttribute('aria-invalid', 'true');
    campo.focus();
  }

  modulo.addEventListener('input', (e) => {
    e.target.removeAttribute?.('aria-invalid');
    errore.hidden = true;
  });

  modulo.addEventListener('submit', (e) => {
    e.preventDefault();
    errore.hidden = true;

    const nome = campoNome.value.trim();
    const giorno = campoGiorno.value;
    const ora = modulo.elements.ora.value;
    const persone = modulo.elements.persone.value;
    const note = modulo.elements.note.value.trim();

    if (!nome) return mostraErrore('Scrivi il nome per la prenotazione.', campoNome);
    if (!giorno) return mostraErrore('Scegli il giorno.', campoGiorno);
    if (giorno < oggiISO) return mostraErrore('Questo giorno è già passato: scegline un altro.', campoGiorno);

    const g = giornoDellaSettimana(giorno);
    if (!orari.get(g)) {
      const articolo = g === 0 ? 'La' : 'Il';
      return mostraErrore(`${articolo} ${GIORNI[g]} siamo chiusi: scegli un altro giorno.`, campoGiorno);
    }

    const dataLeggibile = new Date(`${giorno}T12:00:00`).toLocaleDateString('it-IT', { weekday: 'long', day: 'numeric', month: 'long' });
    const righeMsg = [
      'Ciao! Vorrei prenotare un tavolo al New Livingstone.',
      `Nome: ${nome}`,
      `Giorno: ${dataLeggibile}`,
      `Ora: ${ora}`,
      `Persone: ${persone}`,
    ];
    if (note) righeMsg.push(`Note: ${note}`);
    const testo = righeMsg.join('\n');

    testoMsg.textContent = testo;
    linkWhatsapp.href = `https://wa.me/${WHATSAPP}?text=${encodeURIComponent(testo)}`;
    riepilogo.hidden = false;
    linkWhatsapp.focus();
  });
})();
