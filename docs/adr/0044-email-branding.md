# 0044. OPEN branding and logo in emails

- Status: Proposed
- Date: 2026-11-26

## Context

OPEN's emails use a placeholder style: a white background, Arial, the word "OPEN" in bold plain text, and a green button and grays that are not OPEN colors. Issue #306 asks that every email follow the OPEN brand in `openpickleball/brand-assets`: the ivory palette, the Krona One wordmark with Instrument Sans text, and the OPEN mark. The maintainer decided on #306 that all emails show OPEN's styling and logo, which also answers the brand-assets note that use of the logo needs permission.

The template rules from #36 forbid images and external stylesheets and fonts, so no email can track its reader or load anything it did not ship with. The logo needs one narrow exception. Email clients add their own limits: Gmail and Outlook do not render SVG, most clients ignore web fonts, Outlook on Windows lays out HTML with Word's engine, many clients hide remote images until the reader allows them, and some change colors in dark mode.

Emails are rendered in Celery tasks, with no request, so every URL in them must be absolute and come from settings (as `FRONTEND_URL` does for links).

## Decision

- **One image: the logo, as a hosted PNG.** The OPEN mark in ink (`#17150f`), rasterized from `logo/open-mark-ink.svg` in brand-assets to a PNG at twice its shown size, committed to this repository as a static file and served by WhiteNoise under its hashed name, with WhiteNoise's long cache headers. Emails reference it by an absolute https URL built from a new setting, `BACKEND_URL` (the API's public origin, checked like `FRONTEND_URL`, https when DEBUG is off), joined with `static()`. Every message and every recipient gets the same URL: no query string, fragment, or value per message, so loading it tells OPEN nothing about who opened which email.
- **The `img` tag.** `alt="OPEN"`, explicit `width` and `height` attributes, and `style="display: block; border: 0;"`. The layout must read correctly with images blocked, where the alt text stands in for the mark.
- **No other image, remote resource, or font file.** Still no `<link>`, `@import`, `url(`, `@font-face`, script, tracking pixel, or background image. The hygiene checks (`apps/emails/tests/hygiene.py`) allow exactly one `<img>`, whose `src` equals the logo's URL, and refuse anything else.
- **Fonts as stacks only.** The wordmark: `'Krona One', Arial, Helvetica, sans-serif`, uppercase, `letter-spacing: 0.1em`. Text: `'Instrument Sans', Arial, Helvetica, sans-serif`. Readers who have the fonts see them; everyone else gets Arial.
- **Colors from the ivory palette only.** Background `#f7f3e8`, text `#17150f`, muted `#4d493f`, line `#d8d1bf`, faint `#6c6658`, surface `#fcfaf3`, from brand-assets' `colors.json`. The action button is ink with ivory text. Text and button colors meet WCAG AA contrast. A test checks that every color in a template is one of these.
- **Styles are inline and set in one place.** The shared layout and partials in `templates/emails/` hold the styles. Other templates use them and add no colors or fonts of their own. Styles stay inline (no `<style>` block), since some clients drop it.
- **Layout for Outlook.** The shared layout uses a single-column table (`role="presentation"`) at most 600px wide, which Outlook's Word engine renders correctly.
- **Dark mode.** The layout declares `<meta name="color-scheme" content="light">` and `<meta name="supported-color-schemes" content="light">`, so clients that honor them keep the ivory design. Clients that change colors anyway still get readable text, since the colors are high contrast.
- **The text version.** Each email keeps a plain text version that starts with "OPEN", as today. It has no styling.

## Consequences

- Deployments need `BACKEND_URL`. Without it, settings refuse to start with DEBUG off. `.env.example`, the App Platform spec, and the README list it.
- A reader whose client loads remote images makes one request to OPEN's server for the logo, which shows their IP address and the time in the access logs. The request carries nothing that identifies the reader or the message. Gmail and some other clients fetch images through a proxy, which hides even that.
- A logo change in brand-assets needs a new PNG here. Hashed file names mean old emails keep showing the logo they were sent with, as long as old static files stay served. WhiteNoise serves only the current build's files, so an old email's logo stops loading after a deploy that changes the file, and its alt text shows instead.
- The hygiene checks get one narrow exception, tested on every template and every rendered message.

## Alternatives considered

- Attach the logo to each message and reference it by Content-ID. Rejected: `queue_email()` refuses attachments, every `QueuedEmail` row and message would carry the image, some clients show it as an attachment, and it would change the outbox rules in ADR 0016.
- Embed the logo as a `data:` URI. Rejected: Gmail and Outlook do not show it.
- Use the SVG mark. Rejected: Gmail and Outlook do not render SVG.
- Host the logo on the web app or `openpb.org`. Rejected: another repository would have to keep a file at a fixed path, and a move there would break every email without a failing test here.
- Load Krona One and Instrument Sans as web fonts. Rejected: most clients ignore web fonts, and loading them breaks the no-external-fonts rule.
- Keep the wordmark as text and show no image. Rejected: the maintainer asked for the logo.
