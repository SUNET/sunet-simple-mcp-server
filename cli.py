import sys
import threading
import logging
import os
import subprocess

# 1. AGGRESSIVE PRESIDIO / RICH LOGGER SUPPRESSION
# This captures the exact modules throwing the INFO/WARNING columns in your log
for logger_name in ["presidio-analyzer", "presidio_analyzer", "presidio_analyzer.analyzer_engine", "pymupdf4llm"]:
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.WARNING)  # Silence everything under CRITICAL
    logger.propagate = False           # Stop logs from trickling up to the root handler
    # Remove any pre-existing rich handlers attached by presidio
    while logger.handlers:
        logger.removeHandler(logger.handlers[0])

from mcp_server import mcp, init_server
from filter_state import runtime_config

server_ready = threading.Event()

def print_welcome_banner():
    """Prints the clean header layout, scaled under 80 characters."""
    print("\n================================================================")
    print(" ################ SUNET SIMPLE MCP TEST SERVER ################ ")
    print("[!] INTERACTIVE RUNTIME CLI ACTIVE, TYPE: \'help\' for commands.")
    print("================================================================\n")

def interactive_cli_loop():
    # Waiting for mcp server to fully initilize before printing and asking user input.
    server_ready.wait()
    print_welcome_banner()
    
    while True:
        try:
            # sys.stdin.readline avoids conflicts with standard async loops
            sys.stdout.write("mcp-filter> ")
            sys.stdout.flush()
            line = sys.stdin.readline().strip()
            if not line:
                continue
                
            parts = line.split()
            cmd = parts[0].lower()
            
            if cmd == "help":
                print("\n=========================================================================")
                print("Commands: clients")
                print("          status <client>")
                print("          add/remove <client> <E>")
                print("          token <client> <E> <V> | clear | exit")
                print("=========================================================================\n")
            
            elif cmd == "clients":
                active = runtime_config.get_current_clients()
                print(f"[*] Tracked Clients: {active if active else 'None yet (Defaults will apply)'}")
            
            elif cmd == "status" and len(parts) > 1:
                client_id = parts[1]
                entities, tokens = runtime_config.get_current(client_id)
                print(f"[*] [{client_id}] Active Entities : {entities}")
                print(f"[*] [{client_id}] Replacement Tokens: {tokens}")
                
            elif cmd == "add" and len(parts) > 2:
                client_id = parts[1]
                entity = parts[2].upper()
                entities, tokens = runtime_config.get_current(client_id)
                if entity not in entities:
                    entities.append(entity)
                    runtime_config.update(client_id, entities, tokens)
                    print(f"[+] [{client_id}] Added target entity: {entity}")
                else:
                    print(f"[!] [{client_id}] {entity} already active.")
                    
            elif cmd == "remove" and len(parts) > 2:
                client_id = parts[1]
                entity = parts[2].upper()
                entities, tokens = runtime_config.get_current(client_id)
                if entity in entities:
                    entities.remove(entity)
                    runtime_config.update(client_id, entities, tokens)
                    print(f"[-] [{client_id}] Removed entity: {entity}")
                else:
                    print(f"[!] [{client_id}] {entity} is not in the active.")
                    
            elif cmd == "token" and len(parts) > 3:
                client_id = parts[1]
                entity = parts[2].upper()
                token_val = " ".join(parts[3:]) # Allows tokens with spaces
                entities, tokens = runtime_config.get_current(client_id)
                tokens[entity] = token_val
                runtime_config.update(client_id, entities, tokens)
                print(f"[+] Token mapping saved: {entity} -> {token_val}")
            
            elif cmd == "clear":
                # Clear terminal screen dynamically based on OS
                subprocess.run('cls' if os.name == 'nt' else 'clear')
                print_welcome_banner()
                
            elif cmd == "exit":
                print("[*] Terminating MCP Server...")
                sys.exit(0)
            else:
                print("[!] Command mismatch or missing arguments.")
                print(" Usage: help | status <client> | add/remove <client> <E> | token <client> <E> <V> | clients")
        except Exception as e:
            print(f"[!] CLI Error processing inputs: {e}")

def run_node():
    # 1. Start the runtime CLI parser thread
    cli_thread = threading.Thread(target=interactive_cli_loop, daemon=True)
    cli_thread.start()
    
    # 2. Init global application components
    print("[*] Connecting dependencies to NextCloud instances...")
    init_server()
    
    print("[*] Initializing FastMCP HTTP Stream framework on port 8420...")
    
    # 3. Release the CLI loop thread right before the server starts blocking
    server_ready.set()
    
    # 4. Block on the server loop
    mcp.run(transport="streamable-http")

if __name__ == '__main__':
    run_node()
