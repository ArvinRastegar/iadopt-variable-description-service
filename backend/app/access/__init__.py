"""What a request may do: the access policy (``components/access-policy.md``, gate G-017).

``policy`` holds the pure decision, ``state`` builds a request's access state from the
database, ``dependencies`` guards each route with one capability, and ``admission`` decides
again for a decomposition under the daily limit and reserves its use.
"""
