"""Raggruppa i documenti per argomento e dà un nome a ogni gruppo.

Normalmente non serve lanciarlo a mano: il raggruppamento si rifà da solo dopo
l'indicizzazione, quando i documenti cambiati superano la soglia in
configurazione ("Rifai i temi ogni N documenti"), e dall'interfaccia si forza
con «Rifai i temi» nel menu. Questo comando serve a cambiarne i parametri o a
vedere cosa viene fuori. I nomi dei temi li sceglie il modello di chat.

    python manage.py temi                            # tutte le knowledge
    python manage.py temi --kb mail --gruppi 14      # quanti temi fare
    python manage.py temi --kb mail --senza-modello  # nomi dalle parole dei titoli
    python manage.py temi --kb mail --solo-nuovi     # solo i documenti senza tema
    python manage.py temi --kb mail --quanto-manca   # non cambia niente, informa

Un documento lontano dal centro del suo gruppo resta **senza tema**: meglio
nessuna etichetta che un'etichetta sbagliata.

La logica sta in ``kb/temi.py``, condivisa con lo scatto automatico. Non apre
l'indice vettoriale: si può lanciare con il server acceso.
"""
from django.core.management.base import BaseCommand

from kb import temi
from kb.models import KnowledgeBase


class Command(BaseCommand):
    help = "Assegna un tema a ogni documento raggruppando i vettori già indicizzati."

    def add_arguments(self, parser):
        parser.add_argument("--kb", help="slug della knowledge (default: tutte)")
        parser.add_argument("--gruppi", type=int, default=0,
                            help="quanti temi (default: uno ogni ~30 documenti, fra 4 e 40)")
        parser.add_argument("--soglia", type=float, default=temi.SOGLIA,
                            help="vicinanza minima assoluta al centro del gruppo (0-1)")
        parser.add_argument("--sigma", type=float, default=temi.SIGMA,
                            help="lascia senza tema chi sta più di N deviazioni standard "
                                 "sotto la media del proprio gruppo (0 = disattiva). "
                                 "Serve perché la scala del coseno cambia da un modello "
                                 "di embedding all'altro, mentre questa soglia si adatta.")
        parser.add_argument("--senza-modello", action="store_true", dest="senza_modello",
                            help="ricava i nomi dei gruppi dalle parole dei titoli invece "
                                 "di chiederli al modello: istantaneo, ma i nomi sono "
                                 "molto meno chiari")
        parser.add_argument("--solo-nuovi", action="store_true", dest="solo_nuovi",
                            help="non ricalcola i gruppi: dà un tema ai soli documenti "
                                 "che non ne hanno, copiandolo dal più simile")
        parser.add_argument("--quanto-manca", action="store_true", dest="quanto_manca",
                            help="mostra quanti documenti sono cambiati dall'ultimo "
                                 "raggruppamento, senza toccare niente")
        parser.add_argument("--seed", type=int, default=0, help="seme, per risultati ripetibili")

    def handle(self, *args, **o):
        knowledges = KnowledgeBase.objects.all()
        if o["kb"]:
            knowledges = knowledges.filter(slug=o["kb"])
            if not knowledges:
                self.stderr.write(f"Nessuna knowledge con slug '{o['kb']}'")
                return

        for kb in knowledges:
            if o["quanto_manca"]:
                self._quanto_manca(kb)
            elif o["solo_nuovi"]:
                n = temi.assegna_nuovi(kb)
                self.stdout.write(f"{kb.name}: {n} documenti etichettati per somiglianza")
            else:
                self._raggruppa(kb, o)

    def _quanto_manca(self, kb):
        from kb import config

        cambiati = temi.da_raggruppare(kb)
        ogni = config.get("temi_auto")
        quando = kb.topics_at.strftime("%d/%m/%Y %H:%M") if kb.topics_at else "mai"
        soglia = (f"si rifà da solo a {ogni} (ne mancano {max(0, ogni - cambiati)})"
                  if ogni >= 1 else "scatto automatico disattivato")
        self.stdout.write(f"{kb.name}: ultimo raggruppamento {quando} · "
                          f"{cambiati} documenti cambiati da allora · {soglia}")

    def _raggruppa(self, kb, o):
        riepilogo = temi.raggruppa(
            kb, gruppi=o["gruppi"], soglia=o["soglia"], sigma=o["sigma"],
            senza_modello=o["senza_modello"], seed=o["seed"])
        if not riepilogo:
            self.stdout.write(f"{kb.name}: troppo pochi documenti con vettore, salto.")
            return
        totale = sum(r["totale"] for r in riepilogo)
        assegnati = sum(r["tenuti"] for r in riepilogo)
        self.stdout.write(f"\n{kb.name}: {totale} documenti → {len(riepilogo)} temi")
        for r in riepilogo:
            self.stdout.write(f"  «{r['nome']}» — {r['tenuti']}/{r['totale']} documenti "
                              f"(vicinanza media {r['vicinanza']:.2f})")
        senza = totale - assegnati
        self.stdout.write(self.style.SUCCESS(
            f"  {assegnati} documenti con tema" + (f", {senza} lasciati senza" if senza else "")))
