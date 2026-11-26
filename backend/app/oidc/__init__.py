"""Signing in with ROHub, an OpenID Connect provider (``components/oidc-identity.md``, gate G-020).

``settings`` judges the sign-in switch and the ROHub settings, ``provider`` sends every request to
the provider, ``tokens`` checks what it signed, ``store`` keeps sign-ins under way and ROHub sessions,
and ``flow`` is the trip to ROHub and back behind the routes of ``app.routers.auth``.
"""
