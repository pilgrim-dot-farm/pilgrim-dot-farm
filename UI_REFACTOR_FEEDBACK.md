# UI Feedback

## Mobile
* Play/stop button diameter should be reduced 4 px (collides with top/bottom)
* Strange behavior showing and then hiding the keyboard (extra height added to bottom, some purple some white)
    * Manifests in Chrome
    * See IMG_6224.PNG (then delete it)
    * Safari corrrectly has our page background!
* Chat should be scrolled down upon enter
* Ratings are sometimes incorrectly greyed out when a song is playing
* GRAZE UPON IT is out of place — no need for the element
* We can safely add back "Farm to table slop — We're keeping the house warm." (in proportion)
* Bottom button row:
    * The cyan highlight on the top looks good
    * For highlighted item:
        * Slightly bigger font
        * Cyan on the sides same thickness as up top

## Desktop
* New page starts at the top, but nav pane seems to start at REQUEST LINE
* At what height does the chat begin having scroll bar?

## Both Mobile and Desktop
* Sub-headings are inconsistent:
    * Request line has pink all caps with underline
    * About section has white with a purple shaded box
    * Make all sub-headings like the about section
    * Make all sub-headings centered
* We don't want clicking or tapping on an image to open it

# Extra Request
* We want our cornucopia for paid requests to sparkle
* We have Sparkling Cornucopia Harvest Animation.gif
* What to do:
    1. Ensure the background is truly transparent
    2. Ensure proper cropping
    3. Replace the static cornucopia image with our gif
## Disposition (2026-11-08)

Shipped in the follow-up commit after `9eb706d`:

- **Mobile** — mini PLAY/STOP is 40 px (was 44); the iOS-Chrome keyboard gap
  is dark because `html` now carries `--bg` (the shell still follows `--vvh`);
  entering CHAT snaps the log to the newest message; the SHEEP PEN status
  strip ("GRAZE UPON IT") is gone on phones (the element stays in the DOM,
  hidden, so `chat.js` is untouched); the tagline is back in proportion under
  the one-line logo; the selected tab is 13 px with 2 px cyan left/right
  borders matching the 2 px top border.
- **Queue** — 20 rows per page with a `1–20 of N` prev/next pager (hidden when
  the whole queue fits), so the sparkling cornucopia only ever animates in 20
  rows at a time.
- **Desktop** — a fresh page always highlights RADIO (at `scrollTop ≈ 0` the
  tall-viewport 35 % rule no longer picks REQUEST LINE). Chat scrollbar
  answer: `.chat-log` is `min-height: 180px; max-height: 42vh`, so it scrolls
  once messages exceed 42 % of the window height (302 px at 720 px tall).
- **Both** — every sub-heading now uses the ABOUT treatment (shaded gradient
  box, white, centred): the request line's GROUND RULES / FREE FOR ALL /
  PRIORITY REQUEST / NOW QUEUED / AIRED REQUESTS included. The motivation and
  infra images are no longer links (tap does nothing).
- **Extra** — the static cornucopia is replaced by
  `web/img/cornucopia.webp`: the source GIF cropped to its content
  (472×400 at 20,47), premultiplied before the Lanczos downscale so the white
  RGB under transparent pixels cannot halo, 113×96, 6 frames, 1.32 s, ~33 KB.
  Used for the two hit-counter icons and the paid-queue marker.

**Deferred (not fixed):** the rating row can still grey out while a song is
audible when the state poll's `current_seq` is ahead of the client's audio
clock (and a poll with no `rating` calls `reset()` for the rest of the song).
Recorded as a known issue in `RADIO.md` §9.1. Intended fix: keep the row open
until `pd:nowplaying` moves off the song and let a late click take the
server's 409.

# UI Feedback - Round 2
You'd had the sub-headings' width as the full width of the box.  Let's revert sub-headings to approximately how they were before we started the UI overhaul (pink all caps with an underline whose color I have forgotton and left aligned).

## Paid Request Issue
THE CHECK AND PAY BUTTON DOES NOTHING!  We are supposed to see the background activity (checking for unseemly things, checking copyright), then the pay UI shows up.  Have these steps that involve waiting be enumerated, i.e.:
* (1/3) Checking for unseemly content
* (2/3) Checking for copyright issues
* (3/3) [I don't know all the steps or how many but you get the idea]
  The cancel button always being shown is awkward, but I'm not sure how to mitigate that with the UI broken currently.

I need part of your testing to be submitting a paid request.  There is a string in .env to bypass the payment system.

### Minor Feature Add
With multiple text boxes for input on a priority request, we no longer have an obvious place to put our secret string defined in .env as PAID_OVERRIDE_TOKEN.  Add a fourth step in grey named PROMO CODE listed as optional like COVER ART is.  Have the description be "If you have one, paste it."  Have the text input be single line with no suggested input.

### Paid Request Test
Inputs are covered in the inner headings below.  First enter them as though you don't have a code to bypass payment so that you can ensure the payment UI shows up.  After that, use the promo code so that we can ensure the request goes through.

#### Lyrics
I want a song about how footage of inside the International Space Station (ISS) does not convey the smell.  With up to seven people in that sealed environment, what outshines the Tower of Babel is a tube full of farts and the best we have to offer.  Title should be All Pent Up, and artist should be The Rocketeers.

#### Musical Direction
jazz-swing-big-band

#### Cover Art
A shot of the International Space Station in front of the moon.  Title and artist vertical with title at left and artist at right.

## Other Notes
Take "Help us pay the power and cloud bills" and remove punctuation, wrap it in parenthesis, and make it the same grey as the ground rules.  For the COVER ART description starting with "Direct artistic" to read "Artistic description, e.g. style, colors, scene."

In our "Want to get technical" drop-down, I'm glad we are highlighting the tags for lyrics, but they need to be the same size as the surrounding grey text.  Revise "Always say
## Round 2 disposition (2026-11-08)

- **Sub-headings** reverted to the pre-overhaul look everywhere: pink all-caps
  `16px`/`letter-spacing:2px`, `var(--violet)` underline, left aligned — both
  `.req-list-head` and `.about-head` (no shaded box, no centring).
- **Priority request** — four numbered departments, with the fourth
  (**PROMO CODE**) in grey: single-line input + **APPLY** at the right. The
  code is validated server-side (`POST /api/paid-requests/promo`, constant
  time, never echoed). Two wrong tries grey the box out; a valid code shows
  *Success! This one's on us* in green and the request skips Stripe; failures
  read *FAILED: N attempts remain.* in red. Textarea/input text is `13px`, the
  same as the helper lines. **Both LYRICS and MUSICAL DIRECTION are now
  required** (server + client; `RequestParts.has_song` = both). Cancel is
  gone — the payment UI closes itself. The parenthetical
  *(Help us pay the power and cloud bills)* is unpunctuated and grey; COVER ART
  help now reads *Artistic description, e.g. style, colors, scene.*
- **Enumerated wait states** under the button: *(1/3) Checking for unseemly
  content*, *(2/3) Checking for existing copyrighted material*, *(3/3)
  Preparing secure payment* — active in cyan, done ticked, scrolled into view
  on phones.
- **Root cause of "CHECK IT & PAY does nothing":** a saved `pd_pay_token` from
  an unfinished checkout resumed on the next visit as `awaiting_payment`, which
  locked the whole panel (inputs and button disabled) for up to 90 s. A stale
  token now clears itself on load with *"Your last checkout wasn't finished. If
  you paid, your request is still in the line."* and the form is editable
  immediately. The stage messages also moved above the button so they are not
  below the keyboard/fold.
- **Live test (2026-11-08, production):** new inputs — lyrics
  *"Title: Foghorn Feline. Artist: The Lamp Keepers."* + the lighthouse-cat
  story, music *"1960s garage rock, female vocals, tambourine, fuzzy guitar,
  mid-tempo"*, art *"A black cat at a lighthouse window at dusk, fog rolling
  in, screen-print poster style, teal and orange."*
  1. Without a promo code: all three stages appeared and the embedded Stripe
     checkout mounted (~10 s). Abandoned — no charge.
  2. Wrong codes twice: *FAILED: 1 attempts remain.* then *FAILED: no attempts
     remain.* and both controls disabled.
  3. With the `.env` promo code: *Success! This one's on us* (green), then
     CHECK IT & PAY → *✓ You're in the priority line (#1)* with no checkout
     mount. The queue shows request 587, paid, queued.
