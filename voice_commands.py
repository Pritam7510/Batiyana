"""
BATIYANA Voice Command Parser
Parses and executes voice commands for the shopping app.
"""

import re
from enum import Enum

class CommandType(Enum):
    ADD_PRODUCT = "add_product"
    REMOVE_PRODUCT = "remove_product"
    UPDATE_QUANTITY = "update_quantity"
    CHANGE_QUANTITY = "change_quantity"
    INCREASE_QUANTITY = "increase_quantity"
    DECREASE_QUANTITY = "decrease_quantity"
    CART_ACTION = "cart_action"
    SEARCH = "search"
    NAVIGATE = "navigate"
    EDIT = "edit"
    UTILITY = "utility"
    UNKNOWN = "unknown"

# Synonym mappings for command flexibility
SYNONYMS = {
    'delete': 'remove',
    'erase': 'remove',
    'insert': 'add',
    'increment': 'increase',
    'reduce': 'decrease',
    'basket': 'cart',
    'item': 'product',
    'product': 'item',
    'show': 'search',
    'find': 'search',
    'go to': 'navigate',
    'go': 'navigate',
    'checkout': 'checkout',
    'pay': 'checkout',
    'purchase': 'checkout',
    'save': 'save_cart',
    'cancel': 'cancel_order',
}

CART_ACTIONS = {
    'open cart': ('open', 'cart'),
    'close cart': ('close', 'cart'),
    'show cart': ('show', 'cart'),
    'clear cart': ('clear', 'cart'),
    'empty cart': ('clear', 'cart'),
    'checkout': ('checkout', 'cart'),
    'save cart': ('save', 'cart'),
    'cancel order': ('cancel', 'order'),
}

NAVIGATION_TARGETS = {
    'dashboard': 'home',
    'home': 'home',
    'products': 'products',
    'orders': 'orders',
    'history': 'history',
    'settings': 'profile',
    'back': 'back',
    'list': 'list',
}

UTILITY_COMMANDS = {
    'help': 'help',
    'repeat': 'repeat',
    'stop listening': 'stop',
    'start listening': 'start',
    'refresh': 'refresh',
    'refresh page': 'refresh',
    'logout': 'logout',
    'log out': 'logout',
}

def normalize_text(text):
    """Normalize voice input text."""
    text = text.lower().strip()
    text = re.sub(r'[.,!?;:]', '', text)
    return text

def replace_synonyms(text):
    """Replace synonyms with standard terms."""
    for synonym, standard in SYNONYMS.items():
        text = re.sub(r'\b' + synonym + r'\b', standard, text)
    return text

def extract_quantity(text):
    """Extract quantity number from text."""
    numbers = {
        'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
        'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
        'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
        'fifteen': 15, 'twenty': 20, 'thirty': 30, 'forty': 40,
        'fifty': 50, 'hundred': 100
    }
    
    # Check for digit numbers
    match = re.search(r'\b(\d+)\b', text)
    if match:
        return int(match.group(1))
    
    # Check for word numbers
    for word, num in numbers.items():
        if word in text:
            return num
    
    return 1

def extract_product_name(text, command_keyword):
    """Extract product name from text."""
    # Remove the command keyword and get the remaining text
    pattern = rf'\b{command_keyword}\b\s+(?:(?:the|a|an|product|item)\s+)?'
    remaining = re.sub(pattern, '', text, flags=re.IGNORECASE)
    remaining = remaining.replace('to', '').replace('from', '').strip()
    
    # Clean up the product name
    product_name = re.sub(r'\s+(and|to|from|quantity|items?)\b.*', '', remaining, flags=re.IGNORECASE)
    return product_name.strip().title() if product_name else None

def parse_add_product(text):
    """Parse 'add X product' command."""
    # Pattern: add [quantity] [product]
    match = re.search(r'add\s+(?:(?:the|a|an|product|item)\s+)?(.+?)(?:\s+to\s+(?:my\s+)?(?:list|cart))?$', text, re.IGNORECASE)
    if match:
        full_phrase = match.group(1).strip()
        # Check if quantity is in the phrase
        qty = extract_quantity(full_phrase)
        product_name = extract_product_name(text, 'add')
        return {
            'type': CommandType.ADD_PRODUCT,
            'product': product_name,
            'quantity': qty,
            'confidence': 0.9
        }
    return None

def parse_remove_product(text):
    """Parse 'remove/delete X product' command."""
    match = re.search(r'(?:remove|delete|erase)\s+(?:(?:the|product|item)\s+)?(.+?)(?:\s+from\s+(?:my\s+)?(?:list|cart))?$', text, re.IGNORECASE)
    if match:
        product_name = match.group(1).strip()
        return {
            'type': CommandType.REMOVE_PRODUCT,
            'product': product_name.title(),
            'needs_confirmation': True,
            'confidence': 0.85
        }
    return None

def parse_update_quantity(text):
    """Parse quantity update commands."""
    # Pattern 1: "set X quantity to Y"
    match = re.search(r'(?:set|change|update)\s+(?:(?:the|product|item)\s+)?(.+?)\s+(?:quantity|qty)\s+to\s+(\d+)', text, re.IGNORECASE)
    if match:
        return {
            'type': CommandType.UPDATE_QUANTITY,
            'product': match.group(1).strip().title(),
            'quantity': int(match.group(2)),
            'confidence': 0.95
        }
    
    # Pattern 2: "increase X by Y"
    match = re.search(r'(?:increase|increment)\s+(?:(?:the|product|item)\s+)?(.+?)\s+(?:by|qty)\s+(\d+)', text, re.IGNORECASE)
    if match:
        return {
            'type': CommandType.INCREASE_QUANTITY,
            'product': match.group(1).strip().title(),
            'quantity': int(match.group(2)),
            'confidence': 0.9
        }
    
    # Pattern 3: "decrease X by Y"
    match = re.search(r'(?:decrease|reduce)\s+(?:(?:the|product|item)\s+)?(.+?)\s+(?:by|qty)\s+(\d+)', text, re.IGNORECASE)
    if match:
        return {
            'type': CommandType.DECREASE_QUANTITY,
            'product': match.group(1).strip().title(),
            'quantity': int(match.group(2)),
            'confidence': 0.9
        }
    
    return None

def parse_cart_action(text):
    """Parse cart-related commands."""
    normalized = normalize_text(replace_synonyms(text))
    
    for action_phrase, (action, target) in CART_ACTIONS.items():
        if action_phrase in normalized:
            return {
                'type': CommandType.CART_ACTION,
                'action': action,
                'target': target,
                'confidence': 0.9
            }
    return None

def parse_search(text):
    """Parse search/filter commands."""
    match = re.search(r'(?:search|find|show)\s+(?:for\s+)?(?:my\s+)?(.+?)(?:\s+(?:products?|items?))?$', text, re.IGNORECASE)
    if match:
        query = match.group(1).strip()
        return {
            'type': CommandType.SEARCH,
            'query': query,
            'confidence': 0.85
        }
    return None

def parse_navigation(text):
    """Parse navigation commands."""
    normalized = normalize_text(text)
    
    for target_phrase, page in NAVIGATION_TARGETS.items():
        if target_phrase in normalized:
            return {
                'type': CommandType.NAVIGATE,
                'target': page,
                'confidence': 0.9
            }
    return None

def parse_edit_command(text):
    """Parse edit commands."""
    # Pattern: "rename X to Y"
    match = re.search(r'rename\s+(?:(?:the|product|item)\s+)?(.+?)\s+to\s+(.+?)(?:\s+(?:product|item))?$', text, re.IGNORECASE)
    if match:
        return {
            'type': CommandType.EDIT,
            'action': 'rename',
            'old_name': match.group(1).strip().title(),
            'new_name': match.group(2).strip().title(),
            'confidence': 0.9
        }
    
    # Pattern: "edit X"
    match = re.search(r'edit\s+(?:(?:the|product|item)\s+)?(.+?)$', text, re.IGNORECASE)
    if match:
        return {
            'type': CommandType.EDIT,
            'action': 'edit',
            'product': match.group(1).strip().title(),
            'confidence': 0.8
        }
    
    return None

def parse_utility(text):
    """Parse utility commands."""
    normalized = normalize_text(text)
    
    for cmd_phrase, cmd in UTILITY_COMMANDS.items():
        if cmd_phrase in normalized:
            return {
                'type': CommandType.UTILITY,
                'command': cmd,
                'confidence': 0.95
            }
    return None

def parse_command(transcript):
    """
    Main voice command parser.
    Tries to match the transcript against all known command patterns.
    """
    if not transcript or not isinstance(transcript, str):
        return {'type': CommandType.UNKNOWN, 'confidence': 0.0}
    
    # Normalize and prepare text
    normalized = normalize_text(replace_synonyms(transcript))
    
    # Try parsing in order of specificity (most specific first)
    parsers = [
        parse_update_quantity,  # Most specific quantity patterns
        parse_add_product,
        parse_remove_product,
        parse_edit_command,
        parse_cart_action,
        parse_search,
        parse_navigation,
        parse_utility,
    ]
    
    for parser in parsers:
        result = parser(normalized)
        if result:
            result['original_text'] = transcript
            return result
    
    # Unknown command
    return {
        'type': CommandType.UNKNOWN,
        'original_text': transcript,
        'confidence': 0.0
    }

def get_command_response(command_result, action_result=None):
    """Generate a voice response for the command result."""
    cmd_type = command_result.get('type')
    
    if action_result and action_result.get('ok'):
        # Success responses
        if cmd_type == CommandType.ADD_PRODUCT:
            qty = command_result.get('quantity', 1)
            product = command_result.get('product', 'item')
            return f"{qty} {product}{'s' if qty > 1 else ''} added successfully."
        
        elif cmd_type == CommandType.REMOVE_PRODUCT:
            product = command_result.get('product', 'item')
            return f"{product} removed from your cart."
        
        elif cmd_type == CommandType.UPDATE_QUANTITY:
            product = command_result.get('product', 'item')
            qty = command_result.get('quantity', 0)
            return f"{product} quantity set to {qty}."
        
        elif cmd_type == CommandType.INCREASE_QUANTITY:
            product = command_result.get('product', 'item')
            qty = command_result.get('quantity', 1)
            return f"Increased {product} quantity by {qty}."
        
        elif cmd_type == CommandType.DECREASE_QUANTITY:
            product = command_result.get('product', 'item')
            qty = command_result.get('quantity', 1)
            return f"Decreased {product} quantity by {qty}."
        
        elif cmd_type == CommandType.CART_ACTION:
            action = command_result.get('action', 'action')
            if action == 'checkout':
                return "Checkout completed. Thank you for shopping!"
            elif action == 'clear':
                return "Cart cleared. All items removed."
            else:
                return f"Cart {action}ed successfully."
        
        elif cmd_type == CommandType.SEARCH:
            query = command_result.get('query', 'items')
            return f"Showing results for {query}."
        
        elif cmd_type == CommandType.NAVIGATE:
            target = command_result.get('target', 'page')
            return f"Going to {target}."
        
        elif cmd_type == CommandType.EDIT:
            action = command_result.get('action', 'edit')
            if action == 'rename':
                return f"Renamed to {command_result.get('new_name')}."
            else:
                return f"{command_result.get('product')} ready to edit."
        
        elif cmd_type == CommandType.UTILITY:
            cmd = command_result.get('command', 'command')
            if cmd == 'logout':
                return "Logging out. Goodbye!"
            elif cmd == 'refresh':
                return "Refreshing the page."
            elif cmd == 'help':
                return "Help panel opened. I can help you manage your shopping cart with voice commands."
            else:
                return f"Command {cmd} executed."
    
    elif action_result and not action_result.get('ok'):
        return action_result.get('message', 'Unable to complete the command.')
    
    elif command_result.get('needs_confirmation'):
        product = command_result.get('product', 'this item')
        return f"Are you sure you want to remove {product}? Say yes to confirm."
    
    elif cmd_type == CommandType.UNKNOWN:
        return "Sorry, I didn't understand that command. Please try again."
    
    return "Command processing."
