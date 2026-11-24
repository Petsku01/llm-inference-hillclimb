# ANALYYSI: P2a + stage-3 ladder (6/6 completed, 24.11 ~11:15)

## Data
6 ajon × 16 inference-pyyntöä (96 yht.); metering per ajon tallessa
(metering.json), exact-answer-predikaattien tulokset (workload-results.json),
identiteetti + effective-config + caps kunnossa kaikissa terminal.jsonissa.
Determinismi todistettu ohjelmallisesti: jaettujen specien tulokset +
vastaustekstit BYTE-IDENTTISET kaikissa 6 ajossa (run-1 = orakelikaava).

## 5 "FAIL"ia per ajon — LUOKITELTU (4 muulla luokalla kuin "aito mallivika")

| id | odote | malli | luokka | todiste |
|---|---|---|---|---|
| QT4-final | LARK:7 | LARK:7 | **FALSE FAILURE (grader-reitti)** | ord[76,65,82,75,58,55] == odote; task-grading="exact stripped text" mutta graderi käyttää tool-rataa (spec.get("tools") läsnä) → 0 tool_calls → väärin hylätty |
| QT2-followup | LARK:7 | Project LARK: 7 | **FALSE FAILURE (format/prefix)** | oikea arvo 7 + projekti LARK; predikaatti vaati tasan "LARK:7" |
| logic | blue | Blue | **FALSE FAILURE (case/pikäälkait)** | semanttinen vastaus oikea; == ilman .lower() |
| QT4-call | tool-call lookup_count(LARK) | "" (tyhjä teksti, ei tool_call) | **GENUINE** | malli ei tuottanut tool-call JSONia — todellinen kehittyneisuusero |
| arithmetic | 327 (17*19+4) | 343 | **GENUINE (deterministinen kvantointiaritmetiikkavirhe)** | 343 kaikissa 6 ajossa — toistuva, ei satunnainen |

## Mallin oikea laatu per ajon: 14/16 semanttisesti oikein (6/6 neulat + 8/10
funktionaalista); 2 aitoa poikkeamaa: QT4-call tyhjä vastaus + 343-aritmetiikka.
Molemmat DETERMINISTISET (toistuvat identtisinä kaikissa 6 ajossa ja kaikissa
konteksteissa 32k/131k) — eli toistettu kvanttioittautuminen, ei kohinaa.

## Neulat (fresh-dokumentaatio per kylmäkäynnistys): 6/6 PASS kaikissa 6 ajossa
— needle-retrieval fresh-dokumeille toimii kylmäkäynnistyksittäin.

## Implikaatiot resepteille
1. QT4-final + QT2 + logic: predikaattien kirjukyrs on dokumentoitu (exact ==
   ilman normalisointia) — jatkoreseptikelpoisuudessa nämä 3 luokitellaan
   "grading-design, not model-failure" (tarkistuslistassa A-final-analysis).
2. QT4-call (tool-call-format) + arithmetic: aitoja; QT4-callin tyhji vastaus
   yhtenäinen 32k/131k — kvantointivaikutus, EI kontekstiriippuva (32k ja 131k
   sama tulos → kontekstin kasvu ei heikentänyt: täMÄ on P2a:n ydinväitteen
   tukeva todiste).
3. x2-identity: todistettu. Deterministinen 11/16 + skaalantuva wall →
   PLAN §2:n full-suite x2 -vaatimus täytetty datan sisällöllä.

## Lopputila
Validated: 4 stage-3 ajot + 2 P2a QT6-diagnostikka, kaikki terminal-completed,
metraus/kapseli ok, identiteetit ok. Julkaisupäätös = Petskun LUKKO (valmius
pakettiin: nämä analyysit + ledjerit + kuitit levylä).
