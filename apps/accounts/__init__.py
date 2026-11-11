"""Accounts: the OPEN login model.

This app holds the Account, a registered OPEN login, as the `User` model.
Player profile data (name, ratings, matches, external IDs) does not live
on `User`. It lives on Player, which an Account can claim by linking.
"""
