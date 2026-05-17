from langchain.messages import HumanMessage
from workflow import MessagesState, app

if __name__ == "__main__":
    initial_state = MessagesState(
        messages=[HumanMessage(content="What is the capital of France?")],
        thinking=[],
        purposed_nodes=[],
        purposed_feedbacks=[],
        next_node="manager",
        reasoning="",
        next_purpose="",
        final_reply=None,
    )
    final = app.invoke(initial_state)
    print("Final output:", final)
