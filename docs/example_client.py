"""
Gemini Bridge — client examples
Requires: pip install openai langchain-openai
Bridge running at http://ras:8003 with API_KEY=test
"""

from openai import OpenAI

BRIDGE_URL = "http://ras:8003/v1"
API_KEY = "test"

client = OpenAI(base_url=BRIDGE_URL, api_key=API_KEY)


# ── 1. Simple call (OpenAI SDK, no extra deps) ────────────────────────────────

def ask(question: str) -> str:
    resp = client.chat.completions.create(
        model="gemini",
        messages=[{"role": "user", "content": question}],
    )
    return resp.choices[0].message.content


# ── 2. Streaming ──────────────────────────────────────────────────────────────

def ask_stream(question: str) -> None:
    stream = client.chat.completions.create(
        model="gemini",
        messages=[{"role": "user", "content": question}],
        stream=True,
    )
    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            print(delta, end="", flush=True)
    print()


# ── 3. LangChain — single invoke ──────────────────────────────────────────────

def langchain_ask(question: str) -> str:
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        base_url=BRIDGE_URL,
        api_key=API_KEY,
        model="gemini",
    )
    return llm.invoke(question).content


# ── 4. LangChain — agent with tools ──────────────────────────────────────────

def langchain_agent_example() -> str:
    from langchain_openai import ChatOpenAI
    from langchain.agents import AgentExecutor, create_openai_functions_agent
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
    from langchain_core.tools import tool

    @tool
    def get_weather(city: str) -> str:
        """Return current weather for a city."""
        # stub — replace with real API call
        return f"It is sunny and 22°C in {city}."

    @tool
    def calculator(expression: str) -> str:
        """Evaluate a simple math expression like '2 + 2 * 3'."""
        try:
            return str(eval(expression, {"__builtins__": {}}))
        except Exception as e:
            return f"Error: {e}"

    llm = ChatOpenAI(
        base_url=BRIDGE_URL,
        api_key=API_KEY,
        model="gemini",
    )

    prompt = ChatPromptTemplate.from_messages([
        ("system", "You are a helpful assistant. Use tools when needed."),
        ("human", "{input}"),
        MessagesPlaceholder("agent_scratchpad"),
    ])

    agent = create_openai_functions_agent(llm, [get_weather, calculator], prompt)
    executor = AgentExecutor(agent=agent, tools=[get_weather, calculator], verbose=True)

    result = executor.invoke({"input": "What is the weather in Prague and what is 123 * 456?"})
    return result["output"]


# ── 5. LangChain — simple RAG chain ──────────────────────────────────────────

def langchain_rag_example(docs: list[str], question: str) -> str:
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.runnables import RunnablePassthrough

    llm = ChatOpenAI(base_url=BRIDGE_URL, api_key=API_KEY, model="gemini")

    context = "\n\n".join(docs)

    prompt = ChatPromptTemplate.from_template(
        "Answer the question based only on the context below.\n\n"
        "Context:\n{context}\n\n"
        "Question: {question}"
    )

    chain = (
        {"context": lambda _: context, "question": RunnablePassthrough()}
        | prompt
        | llm
        | StrOutputParser()
    )

    return chain.invoke(question)


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== 1. Direct ask ===")
    print(ask("What is the capital of France?"))

    print("\n=== 2. Streaming ===")
    ask_stream("Count from 1 to 5, one number per line.")

    print("\n=== 3. LangChain invoke ===")
    print(langchain_ask("Explain what a Raspberry Pi is in one sentence."))

    print("\n=== 4. LangChain agent with tools ===")
    print(langchain_agent_example())

    print("\n=== 5. LangChain RAG ===")
    documents = [
        "The GeminiBridge is a FastAPI wrapper around the Gemini CLI.",
        "It exposes an OpenAI-compatible /v1/chat/completions endpoint on port 8003.",
        "It supports both streaming and non-streaming responses.",
    ]
    print(langchain_rag_example(documents, "What port does GeminiBridge run on?"))
