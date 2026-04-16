def test_app_user_model_importable():
    from src.core.models.app_user import AppUser

    u = AppUser(id="usr_1", email="a@b.com")
    assert u.id == "usr_1"
    assert u.email == "a@b.com"


def test_api_key_model_importable():
    from src.core.models.api_key import ApiKey

    k = ApiKey(
        id="01ABC", user_id="usr_1", label="test", key_prefix="co_abc12", key_hash="deadbeef"
    )
    assert k.label == "test"
