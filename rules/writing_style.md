# Rule: Writing Style — No AI Language, No Em Dashes

## Hard rule

Every sentence in every generated application output — resume bullets,
cover letters, screener answers, LinkedIn messages — must read like a
human engineer wrote it. Generic AI phrasing is a gate failure.

---

## Banned: em dashes

Never use `—` (U+2014) or ` — ` in any output.
Use a comma, colon, semicolon, or rewrite the sentence.

| Instead of | Write |
|---|---|
| Built a pipeline — cut latency by 40% | Built a pipeline that cut latency by 40% |
| Trained the model — achieved 93% accuracy | Trained the model, achieving 93% accuracy |

---

## Banned: AI buzzword list

The following words and phrases are **banned in all generated output**.
The style checker will flag them as failures.

### Verbs (use plain alternatives)
| Banned | Use instead |
|---|---|
| leverage / leveraged | used, applied, built on |
| utilize / utilized / utilise | used |
| delve | explore, investigate, examine |
| foster | build, grow, support |
| facilitate | help, enable, support |
| streamline | simplify, speed up, reduce |
| empower | let, enable, allow, give |
| spearhead | led, started, drove |
| orchestrate | ran, coordinated, built |
| harness | use, apply |

### Adjectives / nouns (vague filler)
| Banned | Use instead |
|---|---|
| robust | reliable, fault-tolerant, tested |
| innovative / innovation | new, novel — or describe what's new |
| cutting-edge | state-of-the-art — or name the actual technology |
| transformative | significant, measurable — or name the outcome |
| synergy / synergies | — (delete entirely) |
| paradigm shift | — (delete entirely) |
| seamlessly | — (delete entirely, or describe how) |
| scalable | designed for scale — or name the scale |
| impactful | state the impact in numbers |
| passionate | — (delete; show it through work) |
| excited / thrilled / delighted | — (delete; state the value instead) |
| exceptional / outstanding | — (delete; let the metric speak) |
| world-class | — (delete) |
| ground-breaking | — (delete) |

### Filler phrases (delete entirely)
- "In today's fast-paced world…"
- "In today's landscape…"
- "It is worth noting that…"
- "It is important to mention…"
- "I hope this email finds you well"
- "I am writing to express my interest"
- "I would be thrilled / excited to"
- "I am a hard-working individual"
- "I am a team player"
- "results-driven"
- "detail-oriented"
- "self-starter"
- "go-getter"
- "I look forward to hearing from you" (at resume level — ok in emails)
- "Please don't hesitate to reach out"

---

## What good writing looks like

**Bad (AI output):**
> Leveraged cutting-edge deep learning frameworks to seamlessly facilitate
> robust model training pipelines, fostering innovation and transformative
> outcomes for the team.

**Good (human engineer):**
> Trained a CNN encoder with multi-head attention in JAX, reducing RMSE by
> 15% and enabling cross-resolution generalization across grid configurations.

Rules:
- Past-tense action verbs: built, trained, reduced, improved, integrated,
  deployed, designed, benchmarked, evaluated, extended, wrote, shipped.
- Specific numbers wherever the source_bank has them.
- Short sentences. One idea per bullet.
- Never start a bullet with "Utilized" or "Leveraged".

---

## Enforcement

`src/verifier/style_checker.py` scans all rephrased text for banned patterns
before the verifier gate. Any match is logged and reported in `resume_diff.md`.
Em dash violations are a hard gate failure (same as an unsupported claim).
Buzzword violations are logged as warnings and the phrase is rewritten.
