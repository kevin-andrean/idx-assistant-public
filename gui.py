import gradio as gr
from engine import run_agent


def respond(user_message, history):
    if not user_message.strip():
        return "", history
    response = run_agent(user_message, history)
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": response})
    return "", history


with gr.Blocks(title="IDX Market Assistant") as app:
    gr.Markdown("# IDX Market Assistant")
    gr.Markdown("Your AI-powered Indonesian stock market analyst. Ask about any IDX stock.")

    chatbot = gr.Chatbot(
        height=600,
        show_label=False
    )

    with gr.Row():
        msg = gr.Textbox(
            placeholder="Ask about any IDX stock e.g. 'Is BBCA good for medium term hold?'",
            show_label=False,
            scale=9
        )
        send = gr.Button("Send", scale=1)

    clear = gr.Button("Clear Conversation")

    msg.submit(respond, [msg, chatbot], [msg, chatbot])
    send.click(respond, [msg, chatbot], [msg, chatbot])
    clear.click(lambda: [], None, chatbot)

def main():
    app.launch()

if __name__ == "__main__":
    main()