from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class AgentState(TypedDict):
    message: str


def greeting_node(state: AgentState) -> AgentState:
    """Simple node that adds greeting message to the state."""
    state["message"] = "Hey " + state["message"] + ", how was your day going?"
    return state


def compliment_node(state: AgentState) -> AgentState:
    """A node that adds a compliment message to the state"""
    state["message"] = state["message"] + " you are doing a an amazing job at learning LangGraph"
    return state


# Build the graph
builder = StateGraph(AgentState)
builder.add_node("greeting", greeting_node)  # Add the node
builder.add_node("compliment", compliment_node)
builder.add_edge(START, "greeting")  # Entry from START
builder.add_edge("greeting", "compliment")
builder.add_edge("compliment", END)  # Exit to END

# Compile
graph = builder.compile()

# Run the graph
result = graph.invoke({"message": "Brandon"})
print(result["message"])
# Output: "Hey Brandon, how was your day going?"
