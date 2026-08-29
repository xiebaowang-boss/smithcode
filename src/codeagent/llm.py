from openai import OpenAI

from . import config


class LLMClient:
    def __init__(self):
        self.client = OpenAI(api_key=config.API_KEY, base_url=config.API_BASE)

    def chat(self, messages, tools=None):
        kwargs = {"model": config.MODEL, "messages": messages}
        if tools:
            kwargs["tools"] = [
                {"type": "function", "function": schema} for schema in tools
            ]
        response = self.client.chat.completions.create(**kwargs)

        return response.choices[0].message

    def chat_stream(self, messages, tools=None):
        """流式聊天，返回生成器"""
        kwargs = {
            "model": config.MODEL, 
            "messages": messages,
            "stream": True
        }
        if tools:
            kwargs["tools"] = [
                {"type": "function", "function": schema} for schema in tools
            ]
        
        response = self.client.chat.completions.create(**kwargs)
        
        # 用于累积完整响应
        full_content = ""
        tool_calls_data = []
        
        for chunk in response:
            if chunk.choices[0].delta.content:
                content = chunk.choices[0].delta.content
                full_content += content
                yield content, None  # (内容, 工具调用)
            
            if chunk.choices[0].delta.tool_calls:
                # 处理工具调用（流式中可能分块到达）
                tool_call = chunk.choices[0].delta.tool_calls[0]
                if tool_call.index is not None:
                    # 确保列表足够长
                    while len(tool_calls_data) <= tool_call.index:
                        tool_calls_data.append({
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""}
                        })
                    
                    tc = tool_calls_data[tool_call.index]
                    if tool_call.id:
                        tc["id"] = tool_call.id
                    if tool_call.function.name:
                        tc["function"]["name"] = tool_call.function.name
                    if tool_call.function.arguments:
                        tc["function"]["arguments"] += tool_call.function.arguments
        
        # 返回完整的响应对象
        class StreamResponse:
            def __init__(self, content, tool_calls):
                self.content = content
                self.tool_calls = tool_calls if tool_calls else None
        
        yield StreamResponse(full_content, tool_calls_data if tool_calls_data else None), True


def message_to_dict(message):
    msg = {"role": message.role, "content": message.content or ""}
    if getattr(message, "tool_calls", None):
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in message.tool_calls
        ]
    return msg
