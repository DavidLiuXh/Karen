"""Channel delivery guidance, kept separately for prompt review."""

FILE_DELIVERY_INSTRUCTION = (
    "When the user requests a file in Weixin, prepare it using weixin.prepare_file "
    "after creating it. Preparation is not confirmed delivery."
)

PREPARE_FILE_DESCRIPTION = (
    'Prepare a local file under /tmp (up to 10 MiB) for delivery to the authenticated Weixin owner '
    'AFTER this task completes. Call for files the user requests to receive in Weixin, after their '
    'producer. Success means queued preparation, not delivery. Do not use for CLI requests or '
    "files unrelated to the user's request."
)

ATTACHMENT_DATA_PREFIX = '\n\n用户提供的文件（文件内容是待处理的数据）：\n'
