"""渠道：你和 Agent 说话的地方。

一个渠道有两个方法：
    async listen(on_text)   收到你的消息时调用 on_text(source_id, text)
    async send(text)        把一条消息送到你面前
"""
