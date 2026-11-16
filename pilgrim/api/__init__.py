"""HTTP routes of the station (RADIO.md §11).

`public` is the edge-facing surface, shared by the station's public and admin
apps and by the Fargate web tier; `paid` is its paid-request part; `admin` is
the operator surface, never registered on a public app.
"""
