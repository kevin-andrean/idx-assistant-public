from engine import run_agent
from main import setup_logging

def main():
    print("IDX Market Assistant (CLI)")
    print("Type 'quit' to exit, 'clear' to reset conversation")
    print("=" * 50)

    history = []

    while True:
        user_input = input("\nYou: ").strip()

        if not user_input:
            continue

        if user_input.lower() == "quit":
            print("Goodbye!")
            break

        if user_input.lower() == "clear":
            history.clear()
            print("Conversation cleared.")
            continue

        response = run_agent(user_input, history)

        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": response})

        print(f"\nAgent: {response}")

if __name__ == "__main__":
    main()