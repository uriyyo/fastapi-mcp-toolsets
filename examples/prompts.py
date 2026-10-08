# Prompts: the function's parameters are the prompt's arguments, its docstring their descriptions.
from fastapi import FastAPI

from fastapi_mcp_toolsets import AssistantMessage, MCPAdapter, MCPRouter, UserMessage

adapter = MCPAdapter(name="reviewer")


@adapter.prompt
def explain(code: str) -> str:
    """Ask for an explanation of some code."""
    return f"Explain what this code does:\n\n{code}"


# A list of values completes as a prefix match.
@adapter.prompt(title="Review code", complete={"language": ["python", "rust", "typescript"]})
def review(code: str, language: str = "python") -> list[UserMessage | AssistantMessage]:
    """Ask for a code review.

    Args:
        code: The code to review.
        language: The language it is written in.
    """
    return [
        UserMessage(f"Review this {language} code:\n\n{code}"),
        AssistantMessage("Sure, let me take a look."),
    ]


app = FastAPI()
app.include_router(MCPRouter(adapter))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)
