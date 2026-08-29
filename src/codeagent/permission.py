class Permission:
    def __init__(self):
        self.approved_all = False
        self.session_approved = set()

    def check(self, tool_name: str) -> bool:
        if tool_name in ("read_file", "list_dir"):
            return True
        if self.approved_all or tool_name in self.session_approved:
            return True

        print(f"\n⚠️  Agent 请求执行: {tool_name}")
        answer = input("   允许? [y]是 / [n]否 / [a]本次会话总是允许: ").strip().lower()
        if answer == "a":
            self.session_approved.add(tool_name)
            return True
        return answer == "y"
