# R1 delta #795 — ameesh-cx1, 2026-10-13

Delta 8b7ace1e→ae0400e1 : **GO, B1 fermé, 0 B / 0 N nouveau**.

cmd_interrupt repasse par NAME_RE pour tout sender, y compris args.sender fourni : le préfixe human: est refusé avant mail.send. L'exception d'autorisation humaine a disparu de ce CLI ; le runner conserve son chemin humain provenant des entrées existantes. Le témoin négatif invoque le CLI depuis AGENT_MAIL_NAME=ia avec --from human:smichea, attend code2 / invalid sender et boîte inchangée. Le positif recoupe l'admission/pick du courrier humain, ainsi que le chemin decisions.answer déjà relu. Documentation alignée.

Ce GO ferme le contournement précis de la R1. Pas de nouvel audit de toutes les entrées humaines, aucune suite rejouée, aucune fausse interruption déposée. CI finale non déclarée verte ; aucun feu de fusion.
