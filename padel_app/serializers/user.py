def serialize_user(user):
    if not user:
        return None

    return {
        "id": user.id,
        "name": user.name,
        "username": user.username,
        "email": user.email,
        "phone": user.phone,
        "isActive": user.status == 'active',
        "language": getattr(user, "language", "pt") or "pt",
        "avatarUrl": user.user_image_url,
        # PAD-81: the coach's saved abbreviation wins; otherwise fall back to the
        # initials derived from their name (the previous, always-derived behaviour).
        "abbreviation": user.abbreviation_display,
    }
