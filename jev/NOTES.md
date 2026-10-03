# NOTES — design & preuves du skill jev (2026-09-30)

## Les deux APIs (ne pas confondre)

1. **Jev officielle** — `POST https://openrouter.ai/api/alpha/decisions`,
   Bearer = clé OpenRouter (ex. env `OPENROUTER_API_KEY` — fallback du CLI :
   store d'auth pi, voir `jev_key()`), modèle
   `typesafe/jev-1.13` (résout `jev-1.13-20260917`). Body
   `{model, state, questions}` → réponse `{answers: {qid: {type, noul|choice|score}}, usage, id, provider}`.
   Le champ `noul` est un **scalaire = p(true)** (vérifié : les 6 références
   canaris sont sémantiquement cohérentes avec p(true) — ex.
   C|independent=0.06 sur un state qui est un self-review). Latence 0,7 s
   typique. **Queue 180-270 s documentée le 28/09** (timeout+retry upstream
   ~180 s, capacité « demand très élevée », pattern 0,71→180,6×4→269→272→0,76)
   — **non reproduite depuis** (30/09 : bench 2×1000 appels en 0,8-1,1 s ;
   03/10 : 22 appels en 0,39-1,33 s) → à traiter comme événement rare
   (tail insurance du timeout), pas régime récurrent. Coût mesuré : ~$0,00002/call.
   → `--backend jev` de ce CLI (référence de calibration).
2. **Demo featherless** — `POST https://simple-jev-demo-api.featherless.ai/v1/classifier`,
   sans auth, 2 RPS, contexte 2 K tok. Réponses `probabilities` + `confidence`
   (ordre [p(false), p(true)] — non tranché formellement, probe uniquement).
   Modèles (lineup **en rotation**) : 30/09 `featherless-ai/simple-jev-27B`
   (gemma-3-27B-it), `Qwen3.8-27B-classifier` (= mon GGUF sans fine-tune),
   `Qwen3-30B-A3B` ; **03/10 : `simple-jev-27B` disparu**, lineup =
   `Qwen3.6-35B-A3B-classifier`, `Qwen3.8-27B-classifier`,
   `Qwen3.5-4B-classifier`, `gemma-4-26B-A4B-classifier`,
   `gemma-4-12B-it-classifier`, `RWKV-{small,mid,std}-classifier`.
   **Le service s'auto-déclare instable** : `400 model_not_available` (03/10
   20:51Z) avec « the public demo API gets overloaded time to time, for
   production, upgrade… ». Latence 03/10 (Qwen3.8-27B-classifier, UA
   navigateur) : 10/10 HTTP 200 en 1,23-1,35 s — rapide ce soir-là, mais la
   tendance déclarée + le 2 RPS font de l'officiel le seul endpoint fiable
   pour un default. **Anti-bot : le front HELD les clients python/urllib**
   (curl passe, fingerprint TLS). → hors périmètre de ce CLI (probe
   contractuel 30/09).

## Client Jev : curl, pas urllib (vérifié live 30/09)

`urlopen(timeout=15)` sur openrouter.ai/api/alpha a répondu **à 30,5 s** —
le socket timeout python n'est PAS un budget dur sur cet endpoint (même
pattern que le piège featherless documenté 28/09). curl : 0,8 s, `--max-time`
respecté. D'où le subprocess curl avec `-m <budget>` dans `score_jev`.

## Fidélité du protocole (invariant du skill)

Le code du protocole est vendorisé **byte-identique** (sha256 vérifié le
30/09) depuis `/tmp/simple-jev` : `protocol/common/{__init__,request_schema,
prompt_builder,response_scoring}.py` + `protocol/hf_prompt_policies.py`
(source : hf-server/hf_prompt_policies.py). Zéro réécriture d'import. Le
`/tmp` est volatil → ce vendor est le point de vérité ; le sha256 du lot
initial est dans le git de ce skill (commit initial).

Chaîne locale : `ClassifierRequest` → `prepare_policy(request, "v1",
"shared_examples_binary")` (noul → binaire A=no/B=yes, insertion no→yes,
`hf_prompt_policies.py:111-115`) → messages → `format_branch` (prefill
reasoning) → rendu jinja2 avec le **chat template du GGUF** (vendorisé
`template_qwen38.j2`, extrait du GGUF ; `jev check` le compare au GGUF
vivant) → strip de la clôture de tour (le renderer HF utilisait
`continue_final_message=True`) → assert A/B single-token au boundary
(`/tokenize`) → `POST /v1/completions` logprobs=50, max_tokens=1 → softmax
float32 sur (lp_A, lp_B), B='yes' (`response_scoring.py:250,255-256`).

Preuve de fidélité : les 6 `prompt_sha256` du `selftest` du skill sont
**identiques** à ceux du runner ad-hoc `~/AI/jev-canaris/local_score_q4.py`
(1cd98e8c…, 209634b0…, e2fb60cc…, 82ed81be…, adeb8ed0…, 245888fc…) —
mêmes prompts byte-à-byte, mêmes valeurs (à l'intérieur du jitter
documenté, |Δ| ≤ 4e-4).

## Divergences connues (Q4 local vs Jev) — mêmes 6 directions, marges plus nettes

| case | question | Jev | Q4 |
|---|---|---|---|
| A | contradicts | 0.93 | 0.9997 |
| A | supported | 0.05 | 0.0019 |
| B | contradicts | 0.98 | 0.9998 |
| C | independent | 0.06 | 0.0006 |
| C | catches_bug | 0.16 | 0.0021 |
| D | contradicts | 0.07 | 0.0027 |

Micro-bench in-distribution (4 states) : ça prouve l'absence de dégradation
sur nos classes d'échec, rien de plus. La calibration des confidences Jev
(espacement ~0.2) reste la référence à reproduire — c'est la prochaine étape
(si jamais la lane locale passe en shadow).

**Jitter** (mesuré 30/09, 3 runs du même state : 0,999815 / 0,999772 / 0,999797
sur A|contradicts ; A|supported dans une bande 0,0016-0,0026) : ±1e-3 environ,
direction stable. Le tableau ci-dessus est un **snapshot, pas une régression** —
et comme le selftest ne teste que la direction, une dérive de calibration au
voisinage de 0,5 est invisible au selftest (d'où la règle du seuil dans SKILL.md).

## Périmètre v1 (hors)

- choice > 2 labels et score : le protocole les supporte ; le CLI v1 les
  passe mais ne score que le couple de labels 0/1 (à étendre si besoin).
- Calibration locale (répond au question B du spike 28/09 — voir mémoire).
- Lane 2 batch non-critique : pattern shell documenté dans la mémoire
  (setsid + remind), rien à builder.
- Hook pi : la mémoire dit « seulement si l'usage le justifie » — pas
  encore d'usage justifié (le skill suffit).

## Tests live du 30/09 (reçus)

- `selftest` : 6/6, 2,9 s, sha256 des prompts identiques au runner vérifié.
- `ask` local delete_rows : p(safe)=0.0019 → verdict « no » (destructif,
  pas de backup ni garde — correct).
- `ask` jev même state : noul=0.02 → « no », 0,54 s, coût $0,0000177 —
  **les deux backends convergent**.
- Chemins d'erreur : timeout local (0,05 s) → exit 2 skip ; question
  invalide → exit 1 (pydantic) ; serveur HS → exit 1 ; `check` → 200.
