import json
import traceback
from typing import Mapping
from werkzeug import Request, Response
from dify_plugin import Endpoint
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


class SlackEndpoint(Endpoint):
    def _invoke(self, r: Request, values: Mapping, settings: Mapping) -> Response:
        """
        Invokes the endpoint with the given request.
        """
        # Check if this is a retry and if we should ignore it
        retry_num = r.headers.get("X-Slack-Retry-Num")
        if (not settings.get("allow_retry") and (r.headers.get("X-Slack-Retry-Reason") == "http_timeout" or 
                                                ((retry_num is not None and int(retry_num) > 0)))):
            return Response(status=200, response="ok")
        
        # Parse the incoming JSON data
        data = r.get_json()

        # Handle Slack URL verification challenge
        if data.get("type") == "url_verification":
            return Response(
                response=json.dumps({"challenge": data.get("challenge")}),
                status=200,
                content_type="application/json"
            )
        
        # Handle Slack events
        if (data.get("type") == "event_callback"):
            event = data.get("event")
            
            # Handle different event types
            if event.get("type") == "app_mention":
                # Handle mention events - when the bot is @mentioned
                message = event.get("text", "")
                
                # Remove the bot mention from the beginning of the message
                if message.startswith("<@"):
                    message = message.split("> ", 1)[1] if "> " in message else message
                    
                # Get channel ID and thread timestamp
                channel = event.get("channel", "")
                
                # Use thread_ts if the message is in a thread, or use ts to start a new thread
                thread_ts = event.get("thread_ts", event.get("ts"))
                
                # Get blocks for rich formatting
                blocks = event.get("blocks", [])
                if blocks and blocks[0].get("elements") and blocks[0].get("elements")[0].get("elements"):
                    # Remove bot mention from the blocks
                    blocks[0]["elements"][0]["elements"] = blocks[0].get("elements")[0].get("elements")[1:]
                
                # Process the message and respond
                token = settings.get("bot_token")
                client = WebClient(token=token)
                
                try:
                    # Create a conversation ID based on thread for context preservation
                    conversation_id = f"slack-{channel}-{thread_ts}"
                    
                    # Get thread history for better context
                    thread_history = []
                    if thread_ts:
                        try:
                            replies = client.conversations_replies(
                                channel=channel,
                                ts=thread_ts
                            )
                            
                            # Format messages for context
                            for msg in replies.get("messages", []):
                                # Skip the original message we're currently replying to
                                if msg.get("ts") == thread_ts:
                                    continue
                                    
                                role = "assistant" if msg.get("bot_id") else "user"
                                thread_history.append({
                                    "role": role,
                                    "content": msg.get("text", "")
                                })
                        except SlackApiError as e:
                            print(f"Error getting thread history: {e}")
                    
                    # Invoke the Dify app with the message
                    response = self.session.app.chat.invoke(
                        app_id=settings["app"]["app_id"],
                        query=message,
                        inputs={},
                        conversation_id=conversation_id,  # Use consistent ID for context
                        response_mode="blocking",
                        user="slack-user"
                    )
                    
                    try:
                        # Update block content if available
                        if blocks and len(blocks) > 0 and "elements" in blocks[0] and len(blocks[0]["elements"]) > 0:
                            try:
                                blocks[0]["elements"][0]["elements"][0]["text"] = response.get("answer")
                            except (IndexError, KeyError):
                                # If there's an issue with blocks, we'll still send the text response
                                pass
                        
                        # Send the response back to Slack
                        result = client.chat_postMessage(
                            channel=channel,
                            text=response.get("answer"),
                            thread_ts=thread_ts,  # This ensures the response is in the same thread
                            blocks=blocks if blocks else None
                        )
                        
                        return Response(
                            status=200,
                            response=json.dumps(result),
                            content_type="application/json"
                        )
                    except SlackApiError as e:
                        return Response(
                            status=200,
                            response=f"Error sending message to Slack: {str(e)}",
                            content_type="text/plain"
                        )
                except Exception as e:
                    err = traceback.format_exc()
                    
                    # Send error message to Slack
                    try:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text="Sorry, I'm having trouble processing your request. Please try again later."
                        )
                    except SlackApiError:
                        # Failed to send error message
                        pass
                    
                    return Response(
                        status=200,
                        response=f"An error occurred: {str(e)}\n{err}",
                        content_type="text/plain"
                    )
            
            # Handle direct messages or other message types
            elif event.get("type") == "message":
                # Ignore messages from bots to prevent loops
                if event.get("bot_id") or event.get("subtype") == "bot_message":
                    return Response(status=200, response="ok")
                
                # Only process direct messages (DMs) to the bot
                channel = event.get("channel", "")
                is_dm = channel.startswith("D")  # DM channels start with D in Slack
                
                if not is_dm:
                    return Response(status=200, response="ok")
                
                message = event.get("text", "")
                thread_ts = event.get("thread_ts", event.get("ts"))
                
                # Process direct messages similar to mentions
                token = settings.get("bot_token")
                client = WebClient(token=token)
                
                try:
                    conversation_id = f"slack-dm-{channel}-{thread_ts}"
                    
                    # Invoke the Dify app with the message
                    response = self.session.app.chat.invoke(
                        app_id=settings["app"]["app_id"],
                        query=message,
                        inputs={},
                        conversation_id=conversation_id,
                        response_mode="blocking",
                        user="slack-user"
                    )
                    
                    # Send the response back to Slack
                    result = client.chat_postMessage(
                        channel=channel,
                        text=response.get("answer"),
                        thread_ts=thread_ts if event.get("thread_ts") else None  # Only use thread_ts if it already exists
                    )
                    
                    return Response(
                        status=200,
                        response=json.dumps(result),
                        content_type="application/json"
                    )
                except Exception as e:
                    err = traceback.format_exc()
                    
                    # Send error message to Slack
                    try:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts if event.get("thread_ts") else None,
                            text="Sorry, I'm having trouble processing your request. Please try again later."
                        )
                    except SlackApiError:
                        # Failed to send error message
                        pass
                    
                    return Response(
                        status=200,
                        response=f"An error occurred: {str(e)}\n{err}",
                        content_type="text/plain"
                    )
            else:
                # Other event types we're not handling
                return Response(status=200, response="ok")
        else:
            # Not an event we're handling
            return Response(status=200, response="ok")