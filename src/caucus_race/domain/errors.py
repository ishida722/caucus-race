class DomainError(Exception):
    """ドメインルールに反する操作。CLI ではメッセージをそのまま利用者に示す。"""


class NotFoundError(DomainError):
    pass


class InvalidTransitionError(DomainError):
    pass
