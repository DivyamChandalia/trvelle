"""
Gradio chat interface for the Trvelle travel planning orchestrator.
This application provides a web-based chat interface to interact with the travel planning system.
"""

from trvelle.utils.env_config import load_environment
load_environment()

# Required imports
import gradio as gr
import uuid
import asyncio
import logging
from typing import Optional, Dict, Any

from trvelle.utils.logging_config import configure_logging, get_logger
from trvelle.orchestrator.client import Orchestrator

# Configure logging at the start
configure_logging(console_output=True, log_level=logging.INFO)
logger = get_logger(__name__)

logger.info("-------------------- GRADIO APP START --------------------")

# Initialize the orchestrator
orchestrator = Orchestrator()

def run_gradio_app():
    """Run the Gradio application."""
    
    with gr.Blocks(
        theme=gr.themes.Soft(primary_hue="blue", secondary_hue="sky"), 
        css="footer {display: none !important}",
        title="Trvelle - AI Travel Planner"
    ) as demo:
        
        gr.Markdown(
            "# 🌍 Trvelle - AI Travel Planner\n"
            "Plan your perfect trip with our AI-powered travel assistant! "
            "Ask about flights, hotels, destinations, or create complete itineraries. "
            "Each session maintains conversation history for personalized recommendations."
        )

        # State variables for session management
        user_id_state = gr.State(None)
        chat_id_state = gr.State(None)

        chatbot_component = gr.Chatbot(
            label="Travel Planning Assistant",
            bubble_full_width=False,
            avatar_images=(
                "https://img.icons8.com/ios-glyphs/90/user--v1.png",  # User avatar
                "https://img.icons8.com/external-justicon-flat-justicon/64/external-airplane-transportation-justicon-flat-justicon.png"  # AI travel avatar
            ),
            height=600,
            placeholder="👋 Hello! I'm your AI travel assistant. Ask me about flights, hotels, or help planning your next trip!"
        )

        with gr.Row():
            msg_textbox = gr.Textbox(
                label="Your travel question:",
                placeholder="Ask about flights, hotels, destinations, or say 'Plan a trip to Paris for 5 days'...",
                scale=4,
                autofocus=True,
            )
            submit_button = gr.Button("✈️ Send", variant="primary", scale=1)
        
        with gr.Row():
            clear_button = gr.Button("🗑️ Clear Chat & Start New Session", variant="secondary")
            example_button = gr.Button("💡 Show Examples", variant="secondary")

        # Examples section (initially hidden)
        examples_section = gr.Markdown(
            """
            ### 💡 Example Queries:
            - "Find flights from New York to Paris on June 15th for 2 adults"
            - "Plan a 7-day trip to Tokyo with hotel recommendations"
            - "Search for hotels in London for check-in on July 1st"
            - "Create a complete itinerary for a weekend in Barcelona"
            - "Find the cheapest flight from CDG to NRT on 2025-06-01"
            """,
            visible=False
        )

        async def handle_chat_interaction(
            user_message: str, 
            chat_history: list, 
            current_user_id: Optional[str], 
            current_chat_id: Optional[str]
        ):
            """Handle the chat interaction with the orchestrator."""
            
            if not user_message.strip():
                return "", chat_history, current_user_id, current_chat_id

            # Initialize session IDs if this is a new session
            if current_user_id is None or current_chat_id is None:
                namespace = uuid.NAMESPACE_DNS
                current_user_id = str(uuid.uuid5(namespace, "gradio_user"))
                current_chat_id = str(uuid.uuid5(namespace, f"chat_{uuid.uuid4()}"))
                logger.info(f"New chat session started. User ID: {current_user_id}, Chat ID: {current_chat_id}")

            # Prepare configuration for the orchestrator
            config = {
                "user_id": uuid.UUID(current_user_id), 
                "chat_id": uuid.UUID(current_chat_id)
            }

            try:
                # Add user message to chat history immediately for better UX
                chat_history.append((user_message, "🤔 Thinking..."))
                
                # Call the orchestrator
                response = await orchestrator.orchestrate(user_message, config)
                
                # Extract the AI response content
                ai_response_content = "I apologize, but I encountered an issue processing your request."
                
                if response:
                    if hasattr(response, 'content'):
                        ai_response_content = response.content
                    elif isinstance(response, str):
                        ai_response_content = response
                    elif isinstance(response, dict):
                        # Handle dictionary responses (like final_itinerary)
                        if 'content' in response:
                            ai_response_content = response['content']
                        else:
                            ai_response_content = str(response)
                    elif isinstance(response, list) and len(response) > 0:
                        # Handle list responses (like tool results)
                        if hasattr(response[0], 'content'):
                            ai_response_content = response[0].content
                        else:
                            ai_response_content = str(response[0])
                    else:
                        ai_response_content = str(response)
                
                # Update the last message in chat history with the actual response
                chat_history[-1] = (user_message, ai_response_content)
                
                logger.info(f"Successfully processed user query: {user_message[:50]}...")
                
            except Exception as e:
                error_message = f"I encountered an error while processing your request: {str(e)}"
                chat_history[-1] = (user_message, error_message)
                logger.error(f"Error processing user query: {e}", exc_info=True)

            return "", chat_history, current_user_id, current_chat_id

        def clear_chat_and_reset_session():
            """Clear chat history and reset session IDs."""
            logger.info("Chat cleared by user. A new session will start on the next message.")
            return [], None, None  # Clear chatbot, reset user_id and chat_id

        def toggle_examples():
            """Toggle the visibility of the examples section."""
            return gr.Markdown.update(visible=not examples_section.visible)

        # Connect the Gradio components to the handler functions
        
        # Handle message submission via Enter key in textbox
        msg_textbox.submit(
            handle_chat_interaction,
            inputs=[msg_textbox, chatbot_component, user_id_state, chat_id_state],
            outputs=[msg_textbox, chatbot_component, user_id_state, chat_id_state]
        )
        
        # Handle message submission via button click
        submit_button.click(
            handle_chat_interaction,
            inputs=[msg_textbox, chatbot_component, user_id_state, chat_id_state],
            outputs=[msg_textbox, chatbot_component, user_id_state, chat_id_state]
        )

        # Handle clear chat button
        clear_button.click(
            clear_chat_and_reset_session,
            inputs=[],
            outputs=[chatbot_component, user_id_state, chat_id_state]
        )

        # Handle examples button
        example_button.click(
            toggle_examples,
            inputs=[],
            outputs=[examples_section]
        )

        gr.Markdown(
            "---\n"
            "**🔒 Privacy Note:** Your conversation history is stored securely and used to provide "
            "personalized travel recommendations. Each session uses unique identifiers to maintain "
            "context across your travel planning conversation."
        )

    # Launch the Gradio app
    logger.info("Launching Gradio travel planning app...")
    demo.launch(
        server_name="0.0.0.0",  # Allow external connections
        server_port=7860,       # Default Gradio port
        share=False,            # Set to True if you want a public link
        show_error=True         # Show errors in the interface
    )

# --- Main execution ---
if __name__ == "__main__":
    try:
        run_gradio_app()
    except KeyboardInterrupt:
        logger.info("Application stopped by user")
    except Exception as e:
        logger.error(f"Application failed to start: {e}", exc_info=True)
        raise