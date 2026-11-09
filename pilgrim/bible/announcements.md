# Station Announcements — operator messages (2026-11-07)

Operator-supplied messages injected through the DJ console
(`POST /api/admin/dj/phrases`, docs/systems/console.md §6). A phrase is spoken **verbatim**: no
LLM rewrite. The server prepends the administrator sign-on
(`dj.phrase_prefix` = "This is the administrator,") unless the message already
opens with it, and one phrase is capped at `dj.phrase_max_chars` (280
characters, sign-on included), so longer messages are split into consecutive
parts. Phrases are one-shot: the rendered clip is consumed when placed and
never recycled (AGENTS rule 10).

## Message 1 — 2026-11-07 (operator, verbatim)

> Welcome to Pilgrim Dot Farm — Artificial Intelligence. Natural Stupidity.
> Somehow, this shit works. Everyone hears the same stream. Free requests get
> the barnyard treatment; paid requests skip the barnyard and the line. Vote on
> requests, rate songs with turkeys, and help us keep the house warm. Our
> employees are fueled by electrons and compensated with airflow. The music is
> artificial, the disappointment is authentic, and the bullshit is organic. We
> were going to do this anyway. You're making it better. — Farm Management

### On-air copy (what actually renders)

Profanity is banned on air (AGENTS §1, style guide), so the aired copy
substitutes for the canon's stronger wording, following the same pattern as
tagline line 16 ("bullshit" → "nonsense"):

- "this shit works" → "this works"
- "the bullshit is organic" → "the nonsense is organic"

The em dash after "Pilgrim Dot Farm" becomes a period, and the closing
"— Farm Management" sign-off becomes "From Farm Management.", so the TTS does
not stumble. Split into four phrases (each ≤ 280 chars with the sign-on):

1. Welcome to Pilgrim Dot Farm. Artificial Intelligence. Natural Stupidity.
   Somehow, this works. Everyone hears the same stream.
2. Free requests get the barnyard treatment. Paid requests skip the barnyard
   and the line. Vote on requests, rate songs with turkeys, and help us keep
   the house warm.
3. Our employees are fueled by electrons and compensated with airflow. The
   music is artificial, the disappointment is authentic, and the nonsense is
   organic.
4. We were going to do this anyway. You're making it better. From Farm
   Management.

Rendered as operator_phrase items 9652–9655 (voice `am_justin`, the
administrator's voice).
