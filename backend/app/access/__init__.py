"""What a request may do: the access policy (``components/access-policy.md``, gate G-017).

``policy`` holds the pure decision, ``state`` builds a request's access state from the
database, and ``dependencies`` guards each route with one capability.
"""
