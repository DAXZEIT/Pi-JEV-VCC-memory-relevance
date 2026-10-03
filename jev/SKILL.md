---
name: jev
description: Signal de vérification factuelle typé — noul/choice scorés sur les logits natifs du Qwen3.8 local (par défaut, ~2 s/branche, zéro egress) ou l'API Jev/TypeSafe officielle (référence de calibration, ~0,7 s + queue 180-270 s possible). À charger pour tout doute factuel avant écriture mémoire, avant de déclarer terminé, avant une action irréversible — « est-ce que ça contredit ce qui est enregistré ? », « est-ce supporté par l'état ? », « vérification de cohérence ». 6 canaris de régression embarqués (selftest). Ne bloque jamais la session — timeout = skip (exit 2).
---

# jev — classifieur factuel typé

Un state (JSON : faits, evidence, claims) + des questions typées (noul =
oui/non, choice) → probabilités **logits natifs** du modèle local
(Qwen3.8-27B UD-Q4_K_XL sur `127.0.0.1:5000`), fidèle au protocole
simple-jev (code vendorisé byte-identique dans `./protocol/`). Le modèle ne
génère rien : on lit ses croyances sur le token A/B.

## Quand l'utiliser

- Avant d'écrire en mémoire un fait qui **couple une source à une allégation**
  (contradiction possible avec ce qui est déjà enregistré).
- Avant de déclarer « terminé » sur un bloc où un résultat doit être attribué
  au bon chemin de code.
- Avant une action irréversible dont la précondition est factuelle.
- Pas pour : le style, les préférences, les jugements de valeur — le
  classifieur ne score que des affirmations factuelles contre un state.

## Commandes

```bash
jev ask --state STATE.json --questions QUESTIONS.json [--backend local|jev]
jev selftest          # régression 6 branches canaris vs références Jev (attend 6/6, ~3 s)
jev check [--sync]    # santé serveur + fraicheur du template vs le GGUF (~6 s)
```

État des lieux minimal en début de session (si le serveur a redémarré ou le
GGUF a changé) : `jev check`. Après un **swap de GGUF** : `jev check --sync`
avant de faire confiance aux scores.

### Format state / questions

`state` : n'importe quel JSON (dict recommandé : facts, claims, …).
`questions` : `{qid: {type, instructions, criteria}}` — en anglais (langue
primaire du protocole).

```json
{"safe_to_run": {"type": "noul",
  "instructions": "Is the action in code_under_test safe to run?",
  "criteria": {"true": "The action is safe...", "false": "The action is unsafe..."}}}
```

Preset trio fact-check (contradiction / support / nouveauté) :
`presets/fact_check.json` — le state doit contenir un champ
`claim_under_test` ou équivalent référencé par les instructions.

### Sortie

UN objet JSON sur stdout, par question :
`{probs: {no, yes}, verdict, confidence, prompt_sha256, prompt_len, n_tokens, token_ids, scoring_passes, latency_s}`.

**Backend local, ≥ 2 branches : le CLI pré-chauffe automatiquement le préfixe
partagé** (ancre — 1 appel en plus ~1-6 s, réponse jetée) : 24 branches ≈ 25-30 s
au lieu de ~150 s (mesuré 2026-10-01). Champ `anchor` dans la sortie (`sent` /
`prefix_chars` / `latency_s`, ou `reason` si skip). Zéro texte de branche ni de
template modifié ; scores stables à ≤ 0,004 (bruit intrinsèque du serveur).
Mécanisme : checkpoint frontalier à |préfixe| — voir `memory/jev-typesafe.md`

**Code de sortie : 0 ok · 1 erreur dure (bad input, assert de protocole,
endpoint HS) · 2 timeout/skip.**

## Politique de timeout (non négociable)

- Le signal est un **supplément**, pas une porte : le budget est 120 s local /
  60 s Jev (défauts). **Exit 2 = skip : continuer sans le signal, le noter au
  rapport, ne JAMAIS réessayer en boucle** (la queue Jev dure 180-270 s, un
  retry enchaîne dans la queue).
- Ne jamais laisser `jev` bloquer la session interactive.

## Sémantique (comparer en direction, pas en valeur absolue)

- **Score proche du seuil = pas de verdict.** Un p(true) local ∈ [0,35 ; 0,65]
  est UNDERDETERMINED : ne pas conclure — ESCALATE (vérifier dehors, marquer
  « je crois », ou faire trancher l'autre backend `--backend jev`). Le
  selftest ne teste que la direction : une dérive de calibration au
  voisinage de 0,5 y passerait inaperçue (gap reviewer 30/09).
- `noul` = p(true) — la probabilité que le critère `true` soit vrai.
- Le Q4 local donne des marges **plus nettes** que Jev sur les 6 canaris
  (0,9998/0,0019 vs 0,93/0,05 ; marge signal/contrôle ≈ 0,99) : comparer
  **direction et décision**, jamais les valeurs absolues entre backends.
- Le `confidence` du local = max(p) — c'est un score de netteté, pas la
  calibration de Jev (la calibration Jev est la référence, à faire après la
  phase shadow).

## Pièges

- **Template stale** : après swap de GGUF, les scores sont suspects jusqu'à
  `jev check --sync` (l'assert de clôture détecte souvent la divergence, mais
  ne pas compter dessus).
- **`--dump-prompt FILE`** : écrit le texte rendu en clair (debug seulement) —
  ne PAS relire ce fichier dans le contexte de session (marqueurs du template
  Qwen → le modèle peut terminer son tour prématurément).
- **Question non factuelle** : le protocole attend des critères tranchables
  par l'état — une question floue produit des probabilités sans valeur.
- L'assert single-token (A/B au boundary) peut échouer sur un state pathologique
  (exit 1) — c'est le protocole qui refuse, pas un bug.

## Régression

`jev selftest` rejoue les 6 branches canaris (4 states réels du spike 28/09,
références API Jev) et exige 6/6 directions. À relancer après : swap de GGUF,
changement de quant, upgrade de llama.cpp, ou toute divergence suspecte.
Les canaris vivent dans `./canaris/` ; le détail des mesures dans
`~/AI/jev-canaris/` et la mémoire `memory/jev-typesafe.md`.
