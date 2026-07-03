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
        "abbreviation": "".join(
            [part[0] for part in user.name.split()[:2]]
        ).upper(),
    }
