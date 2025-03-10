import asyncio
import os
import sys

from telethon import TelegramClient, events

import message_handler

# Get API credentials from environment variables
api_id = os.environ.get("TELEGRAM_API_ID")
api_hash = os.environ.get("TELEGRAM_API_HASH")
session_name = os.environ.get("TELEGRAM_SESSION_NAME", "my_session")

# Check if credentials are available
if not api_id or not api_hash:
    raise ValueError("Please set the TELEGRAM_API_ID and TELEGRAM_API_HASH environment variables")

# Convert api_id to integer
api_id = int(api_id)

message_handler.API_ID = api_id
message_handler.API_HASH = api_hash
message_handler.SESSION_NAME = session_name

async def main():
    if len(sys.argv) < 2:
        print("Usage: python main.py [fetch|fetch-unread|analyze|summarize-unread|reply] [chat_id] [query]")
        return
        
    command = sys.argv[1]
    
    # Create a single client for all operations
    client = TelegramClient(session_name, api_id, api_hash)
    await client.start()
    
    try:
        if command == "fetch":
            await message_handler.fetch_messages(client)
        elif command == "fetch-unread":
            await message_handler.fetch_unread_messages(client)
        elif command == "analyze" and len(sys.argv) >= 3:
            chat_id = int(sys.argv[2])
            query = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else None
            await message_handler.analyze_chat(chat_id, query)
        elif command == "summarize-unread" and len(sys.argv) >= 3:
            if sys.argv[2].lower() == "all":
                # Summarize unread messages from all chats
                print("\n=== Summarizing all unread messages across all chats ===\n")
                summary = await message_handler.summarize_all_unread(client)
                print("\n--- Complete Summary of All Unread Messages ---\n")
                print(summary)
            else:
                # Summarize unread messages from a specific chat
                chat_id = int(sys.argv[2])
                summary = await message_handler.summarize_unread(client, chat_id)
                print("\n--- Summary of Unread Messages ---\n")
                print(summary)
        elif command == "reply" and len(sys.argv) >= 3:
            chat_id = int(sys.argv[2])
            await message_handler.generate_reply(client, chat_id)
        else:
            print("Unknown command. Use one of:")
            print("  fetch                       - Fetch all messages")
            print("  fetch-unread                - Fetch only unread messages")
            print("  analyze [chat_id] [query]   - Analyze messages from a chat")
            print("  summarize-unread [chat_id]  - Summarize unread messages from a chat")
            print("  summarize-unread all        - Summarize all unread messages across all chats")
            print("  reply [chat_id]             - Generate and send a reply to a chat")
    finally:
        await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())