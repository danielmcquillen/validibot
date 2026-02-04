"""
Custom authentication classes for Validibot API.

This module provides authentication backends that follow modern API conventions,
specifically using the "Bearer" keyword in Authorization headers instead of the
non-standard "Token" keyword.

Usage:
    Authorization: Bearer <api_token>

This is the OAuth 2.0 standard and what most developers expect when working
with APIs.
"""

from rest_framework.authentication import TokenAuthentication


class BearerAuthentication(TokenAuthentication):
    """
    Token authentication using the Bearer keyword.

    This is a simple subclass of DRF's TokenAuthentication that changes
    the Authorization header keyword from "Token" to "Bearer", following
    the OAuth 2.0 Bearer Token specification (RFC 6750).

    Example header:
        Authorization: Bearer vb_abc123def456

    The underlying token storage and validation remains the same as
    DRF's built-in TokenAuthentication.
    """

    keyword = "Bearer"
