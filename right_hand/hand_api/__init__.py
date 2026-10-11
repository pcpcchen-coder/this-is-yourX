"""Amazing Hand 右手的 skill gateway 與 AI 介面（ADR-0006）。

分層（由下往上）：
  adapter   唯一碰序列埠的地方；真（rustypot）與假（模擬）實作同一組基本操作。
  motion    同時移動、落後與電壓檢查、到位確認；只用 adapter 的基本操作。
  gateway   manifest、前置條件、逐次核准、執行狀態機、稽核。全部是確定性程式。
  daemon    handd：獨占 gateway，開兩個本機 socket（AI 用、操作者用）。
  cli       操作者指令 `hand`。
  mcp_server  給 AI 工具的 MCP stdio server；只會連 handd 的 AI socket。
"""
