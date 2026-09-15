import os
import re
import sqlite3
import threading
import time
import random
import string
import imaplib
import email
from email.header import decode_header
from datetime import datetime, timedelta
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.error import TelegramError
from telegram import ChatMemberAdministrator, ChatMemberOwner, ChatMemberMember, ChatMemberRestricted
import requests
import json
import asyncio
import io
from queue import Queue

# Try to import PIL, make it optional
try:
    from PIL import Image, ImageDraw
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    logging.warning("PIL not available, some features may be limited")

# Configuration - Use environment variables for Plesk
BOT_TOKEN = os.getenv("BOT_TOKEN", "Enter_your_bot_tokken_here")
GMAIL_EMAIL = os.getenv("GMAIL_EMAIL", "enter_your_gmail_name_@gmail.com")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "Enter_Your_gmail_password_here")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "Enter your_openrouter_api_key_here")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "Enter_your_admin_id_here"))
FEEDBACK_CHANNEL_ID = int(os.getenv("FEEDBACK_CHANNEL_ID", "Enter_your_channel_id"))

# Database setup - Use absolute path for Plesk
DB_NAME = os.getenv("DB_PATH", os.path.join(os.getcwd(), "bot.db"))

# Initialize logging for Plesk
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(os.getcwd(), 'bot.log')),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class AliasGenerator:
    """Generate unique and varied Gmail aliases"""
    
    @staticmethod
    def generate_random_alias():
        """Generate a random alias using various formats"""
        formats = [
            AliasGenerator._format_word_number,
            AliasGenerator._format_adjective_noun,
            AliasGenerator._format_mixed_chars,
            AliasGenerator._format_uuid_style,
            AliasGenerator._format_timestamp_style,
            AliasGenerator._format_hex_style,
            AliasGenerator._format_pronounceable,
            AliasGenerator._format_leet_style,
            AliasGenerator._format_camel_case,
            AliasGenerator._format_snake_case
        ]
        
        # Try a few formats to ensure uniqueness
        for _ in range(3):
            chosen_format = random.choice(formats)
            alias = chosen_format()
            
            # Check if alias looks unique enough
            if AliasGenerator._is_unique_enough(alias):
                return alias
        
        # Fallback to UUID style if all else fails
        return AliasGenerator._format_uuid_style()
    
    @staticmethod
    def _format_word_number():
        """Format: word + number (e.g., 'tiger123', 'rocket456')"""
        words = [
            'tiger', 'eagle', 'dragon', 'phoenix', 'thunder', 'lightning',
            'rocket', 'star', 'moon', 'sun', 'cloud', 'storm', 'wave',
            'fire', 'ice', 'wind', 'earth', 'water', 'shadow', 'crystal',
            'diamond', 'golden', 'silver', 'bronze', 'platinum', 'titanium',
            'ninja', 'samurai', 'warrior', 'knight', 'hero', 'legend',
            'swift', 'quick', 'fast', 'rapid', 'instant', 'sudden',
            'magic', 'mystic', 'ancient', 'future', 'quantum', 'cosmic'
        ]
        word = random.choice(words)
        number = random.randint(100, 9999)
        return f"{word}{number}"
    
    @staticmethod
    def _format_adjective_noun():
        """Format: adjective + noun (e.g., 'bravetiger', 'cleverfox')"""
        adjectives = [
            'brave', 'clever', 'swift', 'quick', 'smart', 'wise', 'bold',
            'calm', 'cool', 'epic', 'fast', 'free', 'fresh', 'glad',
            'good', 'great', 'happy', 'kind', 'nice', 'proud', 'safe',
            'strong', 'true', 'wild', 'young', 'eager', 'gentle',
            'honest', 'lucky', 'noble', 'polite', 'quiet', 'rare',
            'rich', 'sharp', 'silly', 'tiny', 'vast', 'warm', 'wise'
        ]
        nouns = [
            'tiger', 'eagle', 'dragon', 'phoenix', 'fox', 'wolf', 'bear',
            'lion', 'hawk', 'falcon', 'owl', 'raven', 'crow', 'swan',
            'dove', 'star', 'moon', 'sun', 'cloud', 'storm', 'wave',
            'fire', 'ice', 'wind', 'earth', 'water', 'shadow', 'light',
            'crystal', 'diamond', 'pearl', 'ruby', 'emerald', 'sapphire',
            'ninja', 'samurai', 'warrior', 'knight', 'hero', 'legend'
        ]
        adjective = random.choice(adjectives)
        noun = random.choice(nouns)
        return f"{adjective}{noun}"
    
    @staticmethod
    def _format_mixed_chars():
        """Format: mix of letters, numbers, and some uppercase (e.g., 'aX7bK9p')"""
        chars = string.ascii_lowercase + string.digits
        # Add some uppercase letters for variety
        uppercase_chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ'  # Exclude confusing letters
        all_chars = chars + uppercase_chars
        
        length = random.randint(6, 10)
        alias = ''.join(random.choices(all_chars, k=length))
        
        # Ensure at least one number and one letter
        if not any(c.isdigit() for c in alias):
            alias = alias[:-1] + random.choice(string.digits)
        if not any(c.isalpha() for c in alias):
            alias = alias[:-1] + random.choice(string.ascii_lowercase)
        
        return alias
    
    @staticmethod
    def _format_uuid_style():
        """Format: UUID-like (e.g., 'a1b2c3d4')"""
        chars = string.ascii_lowercase + string.digits
        parts = []
        for _ in range(4):
            part = ''.join(random.choices(chars, k=2))
            parts.append(part)
        return ''.join(parts)
    
    @staticmethod
    def _format_timestamp_style():
        """Format: timestamp-based (e.g., 'dec31-2345')"""
        months = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 
                  'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
        month = random.choice(months)
        day = random.randint(1, 28)
        number = random.randint(100, 9999)
        return f"{month}{day}-{number}"
    
    @staticmethod
    def _format_hex_style():
        """Format: hexadecimal style (e.g., 'a1f4b8')"""
        hex_chars = '0123456789abcdef'
        length = random.randint(6, 8)
        return ''.join(random.choices(hex_chars, k=length))
    
    @staticmethod
    def _format_pronounceable():
        """Format: pronounceable random strings (e.g., 'zokita', 'mavexi')"""
        consonants = 'bcdfghjklmnpqrstvwxyz'
        vowels = 'aeiou'
        length = random.randint(6, 9)
        alias = ''
        
        for i in range(length):
            if i % 2 == 0:
                alias += random.choice(consonants)
            else:
                alias += random.choice(vowels)
        
        # Add a number at the end
        alias += str(random.randint(1, 99))
        return alias
    
    @staticmethod
    def _format_leet_style():
        """Format: leet speak style (e.g., 'h4ck3r', 'pr0gr4m')"""
        leet_map = {
            'a': '4', 'e': '3', 'i': '1', 'o': '0', 's': '5',
            't': '7', 'l': '1', 'g': '9', 'z': '2'
        }
        
        base_words = [
            'hacker', 'program', 'coder', 'developer', 'system',
            'network', 'server', 'client', 'user', 'admin',
            'master', 'expert', 'pro', 'elite', 'ninja', 'guru'
        ]
        
        word = random.choice(base_words)
        # Convert some letters to leet
        leet_word = ''
        for char in word:
            if char in leet_map and random.random() < 0.5:  # 50% chance to convert
                leet_word += leet_map[char]
            else:
                leet_word += char
        
        # Add number if too short
        if len(leet_word) < 6:
            leet_word += str(random.randint(10, 99))
        
        return leet_word
    
    @staticmethod
    def _format_camel_case():
        """Format: camelCase style (e.g., 'swiftTiger', 'quickFox')"""
        adjectives = [
            'swift', 'quick', 'fast', 'rapid', 'instant', 'sudden',
            'magic', 'mystic', 'ancient', 'future', 'quantum', 'cosmic',
            'brave', 'clever', 'smart', 'wise', 'bold', 'calm'
        ]
        nouns = [
            'tiger', 'eagle', 'dragon', 'phoenix', 'fox', 'wolf',
            'hero', 'star', 'moon', 'sun', 'storm', 'wave'
        ]
        
        adjective = random.choice(adjectives)
        noun = random.choice(nouns)
        
        # Capitalize first letter of noun
        noun = noun.capitalize()
        
        alias = adjective + noun
        
        # Add number if needed
        if random.random() < 0.3:  # 30% chance to add number
            alias += str(random.randint(1, 99))
        
        return alias
    
    @staticmethod
    def _format_snake_case():
        """Format: snake_case style (e.g., 'swift_tiger', 'quick_fox')"""
        adjectives = [
            'swift', 'quick', 'fast', 'rapid', 'instant', 'sudden',
            'magic', 'mystic', 'ancient', 'future', 'quantum', 'cosmic',
            'brave', 'clever', 'smart', 'wise', 'bold', 'calm'
        ]
        nouns = [
            'tiger', 'eagle', 'dragon', 'phoenix', 'fox', 'wolf',
            'hero', 'star', 'moon', 'sun', 'storm', 'wave'
        ]
        
        adjective = random.choice(adjectives)
        noun = random.choice(nouns)
        
        alias = f"{adjective}_{noun}"
        
        # Add number if needed
        if random.random() < 0.3:  # 30% chance to add number
            alias += f"_{random.randint(1, 99)}"
        
        return alias
    
    @staticmethod
    def _is_unique_enough(alias):
        """Check if alias is unique enough (not too simple or repetitive)"""
        # Check minimum length
        if len(alias) < 6:
            return False
        
        # Check for too many repeating characters
        if len(set(alias)) < len(alias) * 0.4:  # Less than 40% unique chars
            return False
        
        # Check for common patterns
        common_patterns = [
            r'^[a-z]+$',  # All lowercase only
            r'^[0-9]+$',  # All numbers only
            r'(.)\1{3,}',  # 4 or more repeating chars
            r'123', r'abc', r'qwe', r'asd', r'zxc'  # Common sequences
        ]
        
        for pattern in common_patterns:
            if re.search(pattern, alias):
                return False
        
        return True

class NotificationQueue:
    """Queue for handling notifications from Gmail thread to bot thread"""
    def __init__(self):
        self.queue = Queue()
        self.bot = None
        self.application = None
    
    def set_bot(self, application):
        """Set the bot application for sending notifications"""
        self.application = application
    
    def add_notification(self, user_id, notification_type, data):
        """Add a notification to the queue"""
        self.queue.put({
            'user_id': user_id,
            'type': notification_type,
            'data': data,
            'timestamp': time.time()
        })
    
    async def process_notifications(self):
        """Process all pending notifications"""
        while not self.queue.empty():
            try:
                notification = self.queue.get_nowait()
                await self._send_notification(notification)
            except Exception as e:
                logger.error(f"Error processing notification: {e}")
    
    async def _send_notification(self, notification):
        """Send a single notification"""
        if not self.application:
            logger.warning("Bot application not set, cannot send notification")
            return
        
        try:
            user_id = notification['user_id']
            notification_type = notification['type']
            data = notification['data']
            
            if notification_type == 'otp':
                await self._send_otp_notification(user_id, data)
            elif notification_type == 'message':
                await self._send_message_notification(user_id, data)
                
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")
    
    async def _send_otp_notification(self, user_id, data):
        """Send OTP notification to user"""
        try:
            alias_name = data.get('alias_name')
            otp_code = data.get('otp_code')
            verification_links = data.get('verification_links', [])
            subject = data.get('subject', 'No Subject')
            
            # Create notification message
            notification = f"🔔 **New OTP/Verification Received!**\n\n"
            notification += f"📧 **Alias:** `{alias_name}`\n"
            notification += f"📨 **Subject:** {subject}\n\n"
            
            if otp_code:
                notification += f"🔑 **OTP Code:** `{otp_code}`\n\n"
            
            if verification_links:
                notification += "🔗 **Verification Links:**\n"
                for i, link in enumerate(verification_links[:3]):  # Show max 3 links
                    notification += f"• [Link {i+1}]({link})\n"
                notification += "\n"
            
            notification += "💡 *Use /otp to view all codes*\n"
            notification += "⏰ *Auto-expires in 1 hour*"
            
            # Create inline keyboard
            keyboard = []
            row = []
            
            if otp_code:
                row.append(InlineKeyboardButton("📋 Copy OTP", callback_data=f"quick_copy_otp_{otp_code}"))
            
            if verification_links:
                row.append(InlineKeyboardButton("🔗 Copy Link", callback_data=f"quick_copy_link_{verification_links[0][:30]}"))
            
            if row:
                keyboard.append(row)
            
            keyboard.append([
                InlineKeyboardButton("👀 View Messages", callback_data=f"view_{alias_name}"),
                InlineKeyboardButton("🔑 All OTPs", callback_data="view_otp")
            ])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Send notification
            await self.application.bot.send_message(
                chat_id=user_id,
                text=notification,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            
            logger.info(f"Sent OTP notification to user {user_id} for alias {alias_name}")
            
        except Exception as e:
            logger.error(f"Failed to send OTP notification: {e}")
    
    async def _send_message_notification(self, user_id, data):
        """Send regular message notification"""
        try:
            alias_name = data.get('alias_name')
            subject = data.get('subject', 'No Subject')
            
            notification = f"📧 **New Email Received**\n\n"
            notification += f"📧 **Alias:** `{alias_name}`\n"
            notification += f"📨 **Subject:** {subject}\n\n"
            notification += "💡 *Use /view to read the message*"
            
            await self.application.bot.send_message(
                chat_id=user_id,
                text=notification,
                parse_mode='Markdown'
            )
            
        except Exception as e:
            logger.error(f"Failed to send message notification: {e}")

# Global notification queue
notification_queue = NotificationQueue()

class DatabaseManager:
    def __init__(self):
        self.db_path = DB_NAME
        self.conn = None
        self.connect()
        self.create_tables()
    
    def connect(self):
        """Connect to database with retry logic"""
        max_retries = 5
        for attempt in range(max_retries):
            try:
                self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
                logger.info(f"Connected to database at {self.db_path}")
                return True
            except Exception as e:
                logger.error(f"Database connection attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)  # Exponential backoff
                else:
                    raise e
        return False
    
    def create_tables(self):
        cursor = self.conn.cursor()
        
        # Users table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                banned BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Aliases table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS aliases (
                alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                alias_name TEXT UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        # Add active column if it doesn't exist
        cursor.execute('PRAGMA table_info(aliases)')
        columns = [col[1] for col in cursor.fetchall()]
        if 'active' not in columns:
            cursor.execute('ALTER TABLE aliases ADD COLUMN active BOOLEAN DEFAULT TRUE')
            logger.info("Added 'active' column to aliases table")
        
        # Messages table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                alias_id INTEGER,
                email_subject TEXT,
                email_body TEXT,
                received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                seen BOOLEAN DEFAULT FALSE,
                otp_code TEXT,
                verification_links TEXT,
                FOREIGN KEY (alias_id) REFERENCES aliases (alias_id)
            )
        ''')
        
        # Add otp_code and verification_links columns if they don't exist
        cursor.execute('PRAGMA table_info(messages)')
        message_columns = [col[1] for col in cursor.fetchall()]
        if 'otp_code' not in message_columns:
            cursor.execute('ALTER TABLE messages ADD COLUMN otp_code TEXT')
            logger.info("Added 'otp_code' column to messages table")
        if 'verification_links' not in message_columns:
            cursor.execute('ALTER TABLE messages ADD COLUMN verification_links TEXT')
            logger.info("Added 'verification_links' column to messages table")
        
        # Feedback table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS feedback (
                feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                feedback_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        # Check and add feedback_photo_id column if it doesn't exist
        cursor.execute('PRAGMA table_info(feedback)')
        feedback_columns = [col[1] for col in cursor.fetchall()]
        if 'feedback_photo_id' not in feedback_columns:
            cursor.execute('ALTER TABLE feedback ADD COLUMN feedback_photo_id TEXT')
            logger.info("Added 'feedback_photo_id' column to feedback table")
        
        self.conn.commit()
    
    def add_user(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('INSERT OR IGNORE INTO users (user_id) VALUES (?)', (user_id,))
        self.conn.commit()
    
    def is_user_banned(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT banned FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        return result and result[0]
    
    def ban_user(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('UPDATE users SET banned = TRUE WHERE user_id = ?', (user_id,))
        self.conn.commit()
    
    def unban_user(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('UPDATE users SET banned = FALSE WHERE user_id = ?', (user_id,))
        self.conn.commit()
    
    def add_alias(self, user_id, alias_name):
        cursor = self.conn.cursor()
        alias_name = alias_name.lower()  # Store aliases in lowercase
        try:
            cursor.execute('INSERT INTO aliases (user_id, alias_name, active) VALUES (?, ?, TRUE)', (user_id, alias_name))
            self.conn.commit()
            logger.info(f"Added alias: {alias_name} for user: {user_id}")
            return True
        except sqlite3.IntegrityError:
            logger.warning(f"Alias {alias_name} already exists for user: {user_id}")
            return False
    
    def get_user_aliases(self, user_id, limit=10):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT alias_name, created_at, active 
            FROM aliases 
            WHERE user_id = ? 
            ORDER BY created_at DESC 
            LIMIT ?
        ''', (user_id, limit))
        return cursor.fetchall()
    
    def set_alias_active(self, user_id, alias_name, active=True):
        cursor = self.conn.cursor()
        alias_name = alias_name.lower()
        cursor.execute('''
            UPDATE aliases 
            SET active = ? 
            WHERE user_id = ? AND alias_name = ?
        ''', (active, user_id, alias_name))
        self.conn.commit()
        return cursor.rowcount > 0
    
    def get_active_aliases(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT alias_name 
            FROM aliases 
            WHERE user_id = ? AND active = TRUE
        ''', (user_id,))
        return [row[0] for row in cursor.fetchall()]
    
    def add_message(self, alias_name, subject, body, otp_code=None, verification_links=None):
        cursor = self.conn.cursor()
        alias_name = alias_name.lower()
        
        # Convert links list to JSON string
        links_json = json.dumps(verification_links) if verification_links else None
        
        cursor.execute('''
            INSERT INTO messages (alias_id, email_subject, email_body, received_at, otp_code, verification_links)
            SELECT alias_id, ?, ?, CURRENT_TIMESTAMP, ?, ?
            FROM aliases 
            WHERE alias_name = ? AND active = TRUE
        ''', (subject, body, otp_code, links_json, alias_name))
        if cursor.rowcount > 0:
            logger.info(f"Added message for alias: {alias_name}, subject: {subject}, OTP: {otp_code}")
            self.conn.commit()
            
            # Get user_id for notification
            cursor.execute('''
                SELECT user_id 
                FROM aliases 
                WHERE alias_name = ? AND active = TRUE
            ''', (alias_name,))
            result = cursor.fetchone()
            
            if result:
                user_id = result[0]
                # Add to notification queue
                if otp_code or verification_links:
                    notification_queue.add_notification(user_id, 'otp', {
                        'alias_name': alias_name,
                        'subject': subject,
                        'otp_code': otp_code,
                        'verification_links': verification_links
                    })
                else:
                    notification_queue.add_notification(user_id, 'message', {
                        'alias_name': alias_name,
                        'subject': subject
                    })
        else:
            logger.warning(f"Failed to add message for alias: {alias_name} (alias not found or inactive)")
    
    def get_recent_messages(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT m.message_id, a.alias_name, m.email_subject, m.email_body, m.received_at, m.seen, m.otp_code, m.verification_links
            FROM messages m
            JOIN aliases a ON m.alias_id = a.alias_id
            WHERE a.user_id = ? AND a.active = TRUE
            ORDER BY m.received_at DESC
        ''', (user_id,))
        messages = cursor.fetchall()
        logger.info(f"Retrieved {len(messages)} messages for user: {user_id}")
        return messages
    
    def mark_message_seen(self, message_id):
        cursor = self.conn.cursor()
        cursor.execute('UPDATE messages SET seen = TRUE WHERE message_id = ?', (message_id,))
        self.conn.commit()
    
    def cleanup_old_messages(self, seconds=3600):  # 1 hour for testing
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM messages WHERE received_at < datetime("now", ?)', (f'-{seconds} seconds',))
        self.conn.commit()
    
    def save_feedback(self, user_id, feedback_text=None, feedback_photo_id=None):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO feedback (user_id, feedback_text, feedback_photo_id)
            VALUES (?, ?, ?)
        ''', (user_id, feedback_text, feedback_photo_id))
        self.conn.commit()
        return cursor.lastrowid
    
    # Statistics methods
    def get_user_count(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM users')
        return cursor.fetchone()[0]
    
    def get_banned_user_count(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM users WHERE banned = TRUE')
        return cursor.fetchone()[0]
    
    def get_alias_count(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM aliases')
        return cursor.fetchone()[0]
    
    def get_active_alias_count(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM aliases WHERE active = TRUE')
        return cursor.fetchone()[0]
    
    def get_message_count(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM messages')
        return cursor.fetchone()[0]
    
    def get_all_user_ids(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT user_id FROM users WHERE banned = FALSE')
        return [row[0] for row in cursor.fetchall()]

class GmailManager:
    def __init__(self, db_manager):
        self.db = db_manager
        self.imap = None
        self.polling = False
        self.poll_thread = None
    
    def connect_gmail(self):
        try:
            self.imap = imaplib.IMAP4_SSL("imap.gmail.com")
            self.imap.login(GMAIL_EMAIL, GMAIL_APP_PASSWORD)
            logger.info("Successfully connected to Gmail IMAP")
            return True
        except Exception as e:
            logger.error(f"Gmail connection failed: {e}")
            return False
    
    def start_polling(self):
        if self.polling:
            return
        
        self.polling = True
        self.poll_thread = threading.Thread(target=self._poll_emails, daemon=True)
        self.poll_thread.start()
    
    def stop_polling(self):
        self.polling = False
        if self.imap:
            try:
                self.imap.close()
                self.imap.logout()
            except:
                pass
    
    def _poll_emails(self):
        attempt = 0
        while self.polling:
            try:
                if not self.imap:
                    if not self.connect_gmail():
                        logger.error("Failed to reconnect to Gmail. Retrying in 30 seconds.")
                        time.sleep(30)
                        attempt += 1
                        continue
                
                self.imap.select("INBOX")
                # Search for unseen emails
                status, messages = self.imap.search(None, 'UNSEEN')
                
                if status != "OK":
                    logger.error(f"IMAP search failed: {status}")
                    time.sleep(5)
                    attempt += 1
                    continue
                
                email_ids = messages[0].split()
                if not email_ids:
                    logger.info("No new emails found.")
                
                for email_id in email_ids:
                    status, msg_data = self.imap.fetch(email_id, "(RFC822)")
                    
                    if status != "OK":
                        logger.error(f"Failed to fetch email ID {email_id}")
                        continue
                    
                    raw_email = msg_data[0][1]
                    email_message = email.message_from_bytes(raw_email)
                    
                    # Extract email details
                    subject = self._decode_header(email_message["Subject"])
                    from_addr = email_message["From"]
                    to_addr = email_message["To"] or ""
                    
                    logger.info(f"Processing email to: {to_addr}")
                    
                    # Match alias (case-insensitive)
                    alias_match = re.search(r'9411revop2\+([^@]+)@gmail\.com', to_addr, re.IGNORECASE)
                    if alias_match:
                        alias_name = alias_match.group(1).lower()
                        logger.info(f"Found alias: {alias_name}")
                        
                        cursor = self.db.conn.cursor()
                        cursor.execute('SELECT alias_id FROM aliases WHERE alias_name = ? AND active = TRUE', (alias_name,))
                        if cursor.fetchone():
                            body = self._extract_body(email_message)
                            
                            # Extract OTP and verification links
                            otp_service = OTPService()
                            otp_code = otp_service.extract_otp(body)
                            verification_links = otp_service.extract_verification_links(body)
                            
                            # Save with extracted data (this will trigger notification)
                            self.db.add_message(alias_name, subject, body, otp_code, verification_links)
                            
                            logger.info(f"Stored message for alias: {alias_name}, OTP: {otp_code}")
                        
                        self.imap.store(email_id, '+FLAGS', '\\Seen')
                    else:
                        logger.info(f"No alias match for email to: {to_addr}")
                        # Optionally mark non-alias emails as seen if desired
                        # self.imap.store(email_id, '+FLAGS', '\\Seen')
                
                self.db.cleanup_old_messages(3600)  # 1 hour for testing
                
                attempt = 0
                
            except Exception as e:
                logger.error(f"Error polling emails: {e}")
                self.imap = None
                attempt += 1
                time.sleep(min(60, 5 * (2 ** attempt)))  # Exponential backoff
            
            time.sleep(5)
    
    def _decode_header(self, header):
        if header:
            decoded_parts = decode_header(header)
            decoded_header = ""
            for part, encoding in decoded_parts:
                if isinstance(part, bytes):
                    if encoding:
                        decoded_header += part.decode(encoding)
                    else:
                        decoded_header += part.decode('utf-8', errors='ignore')
                else:
                    decoded_header += part
            return decoded_header
        return ""
    
    def _extract_body(self, email_message):
        body = ""
        html_body = ""
        
        if email_message.is_multipart():
            for part in email_message.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))
                
                if "attachment" in content_disposition:
                    continue
                
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                
                try:
                    if content_type == "text/plain":
                        body += payload.decode('utf-8', errors='ignore') + "\n"
                    elif content_type == "text/html":
                        html_body += payload.decode('utf-8', errors='ignore') + "\n"
                except Exception as e:
                    logger.error(f"Error decoding part: {e}")
        
        else:
            payload = email_message.get_payload(decode=True)
            if payload:
                content_type = email_message.get_content_type()
                try:
                    if content_type == "text/plain":
                        body = payload.decode('utf-8', errors='ignore')
                    elif content_type == "text/html":
                        html_body = payload.decode('utf-8', errors='ignore')
                except Exception as e:
                    logger.error(f"Error decoding payload: {e}")
        
        # Combine all plain text parts
        if body:
            return body
        
        # If only HTML, return it
        if html_body:
            return html_body
        
        return ""

class OTPService:
    @staticmethod
    def extract_otp(email_body):
        try:
            # Enhanced OTP patterns
            otp_patterns = [
                # Common OTP patterns
                r'\b\d{6}\b',
                r'\b\d{4}\b',
                r'\b\d{5}\b',
                r'\b\d{8}\b',
                
                # OTP with labels
                r'(?:OTP|code|verification|pin|security code)[\s:]*([0-9]{4,8})',
                r'(?:one[-\s]?time[-\s]?pass|password)[\s:]*([0-9]{4,8})',
                
                # In brackets
                r'\[([0-9]{4,8})\]',
                r'\(([0-9]{4,8})\)',
                r'\{([0-9]{4,8})\}',
                
                # With quotes
                r'["\']([0-9]{4,8})["\']',
                
                # Common patterns
                r'Your code is[:\s]*([0-9]{4,8})',
                r'Enter[:\s]*([0-9]{4,8})',
                r'Use[:\s]*([0-9]{4,8})',
                
                # WhatsApp patterns
                r'([0-9]{3}[-\s]?[0-9]{3})',  # 6-digit with dash/space
                
                # Generic number patterns (less likely to be false positives)
                r'\b(?:1[0-9]{9}|[2-9][0-9]{8})\b',  # 10-digit numbers
            ]
            
            # Try each pattern
            for pattern in otp_patterns:
                matches = re.findall(pattern, email_body, re.IGNORECASE)
                if matches:
                    # Return the first match that looks like an OTP
                    for match in matches:
                        # Ensure it's a pure number or has reasonable length
                        clean_match = re.sub(r'[^\d]', '', str(match))
                        if 4 <= len(clean_match) <= 8:
                            return clean_match
            
            # If no pattern matches, try AI extraction
            return OTPService._extract_otp_ai(email_body)
            
        except Exception as e:
            logger.error(f"OTP extraction error: {e}")
            return None
    
    @staticmethod
    def _extract_otp_ai(email_body):
        try:
            prompt = f"""Extract any OTP, verification code, or security code from this email body. 
            Return ONLY the code as digits, nothing else. If no code found, return 'NONE'.
            
            Email body: {email_body[:1000]}"""
            
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "mistralai/mistral-7b-instruct",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 10,
                    "temperature": 0.1
                },
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()["choices"][0]["message"]["content"].strip()
                if result.upper() != "NONE" and result.isdigit():
                    return result
            
            return None
        except Exception as e:
            logger.error(f"AI OTP extraction failed: {e}")
            return None

    @staticmethod
    def extract_verification_links(body):
        try:
            # Enhanced link extraction patterns
            all_links = []
            
            # Pattern 1: Standard URLs
            url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
            urls = re.findall(url_pattern, body, re.IGNORECASE)
            all_links.extend(urls)
            
            # Pattern 2: Href attributes
            href_pattern = r'href=[\'"]?([^\'" >]+)'
            hrefs = re.findall(href_pattern, body, re.IGNORECASE)
            all_links.extend(hrefs)
            
            # Pattern 3: Common verification domains
            verification_domains = [
                'verify', 'confirm', 'activate', 'auth', 'login', 'signin',
                'reset', 'recover', 'verifyemail', 'emailverify', 'click',
                'link', 'redirect', 'continue', 'proceed', 'access'
            ]
            
            # Pattern 4: Links with verification keywords
            for domain in verification_domains:
                pattern = rf'https?://[^\s]*{domain}[^\s]*'
                matches = re.findall(pattern, body, re.IGNORECASE)
                all_links.extend(matches)
            
            # Pattern 5: Links with common parameters
            param_patterns = [
                r'https?://[^\s]*\?(?:[^&]*&)*?(?:token|code|key|id|hash|sig|verify|confirm)=[^\s&]+',
                r'https?://[^\s]*\?(?:[^&]*&)*?(?:utm_|campaign|source|medium)=[^\s&]+',
            ]
            
            for pattern in param_patterns:
                matches = re.findall(pattern, body, re.IGNORECASE)
                all_links.extend(matches)
            
            # Clean and deduplicate links
            cleaned_links = []
            seen = set()
            
            for link in all_links:
                # Clean up the link
                link = link.strip('.,;:!?)')
                
                # Skip if too short or already seen
                if len(link) < 10 or link in seen:
                    continue
                
                # Skip common non-verification links
                skip_patterns = [
                    r'facebook\.com',
                    r'twitter\.com',
                    r'instagram\.com',
                    r'youtube\.com',
                    r'linkedin\.com',
                    r'google\.com/search',
                    r'unsubscribe',
                    r'preferences',
                    r'privacy',
                    r'terms',
                ]
                
                should_skip = False
                for pattern in skip_patterns:
                    if re.search(pattern, link, re.IGNORECASE):
                        should_skip = True
                        break
                
                if not should_skip:
                    seen.add(link)
                    cleaned_links.append(link)
            
            # Prioritize links that look like verification links
            verification_links = []
            other_links = []
            
            for link in cleaned_links:
                # Check if it looks like a verification link
                if any(keyword in link.lower() for keyword in verification_domains):
                    verification_links.append(link)
                elif '?' in link and len(link) > 50:
                    verification_links.append(link)
                else:
                    other_links.append(link)
            
            # Return verification links first, then other links
            return verification_links[:5] + other_links[:5]  # Max 10 links total
            
        except Exception as e:
            logger.error(f"Link extraction error: {e}")
            return []

class TelegramBot:
    def __init__(self):
        self.db = DatabaseManager()
        self.gmail = GmailManager(self.db)
        self.otp_service = OTPService()
        self.application = None
        self.feedback_users = set()  # Track users in feedback mode
        self.user_data = {}  # Store temporary user data
        self.otp_notifications = {}  # Track OTP notifications sent
    
    async def check_channel_permissions(self, context: ContextTypes.DEFAULT_TYPE):
        """Check bot permissions in the feedback channel"""
        try:
            # Get chat member info to check permissions
            chat_member = await context.bot.get_chat_member(
                chat_id=FEEDBACK_CHANNEL_ID,
                user_id=context.bot.id
            )
            
            # Check different types of chat members
            if isinstance(chat_member, (ChatMemberAdministrator, ChatMemberOwner)):
                # Admins and owners automatically have all permissions
                logger.info("Bot is an administrator/owner in the channel")
                return True
            elif isinstance(chat_member, ChatMemberMember):
                # Regular member - check if they can send messages
                if hasattr(chat_member, 'can_send_messages') and chat_member.can_send_messages:
                    logger.info("Bot is a member with send messages permission")
                    return True
                else:
                    logger.error("Bot is a member but cannot send messages")
                    return False
            elif isinstance(chat_member, ChatMemberRestricted):
                # Restricted member - check specific permissions
                if hasattr(chat_member, 'can_send_messages') and chat_member.can_send_messages:
                    logger.info("Bot is restricted but can send messages")
                    return True
                else:
                    logger.error("Bot is restricted and cannot send messages")
                    return False
            else:
                # Left or kicked
                logger.error(f"Bot status in channel: {chat_member.status}")
                return False
        except TelegramError as e:
            logger.error(f"Error checking channel permissions: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error checking channel permissions: {e}")
            return False
    
    async def check_channel_access(self, context: ContextTypes.DEFAULT_TYPE):
        """Check if bot has access to the feedback channel"""
        try:
            chat = await context.bot.get_chat(FEEDBACK_CHANNEL_ID)
            logger.info(f"Bot has access to channel: {chat.title}")
            return True
        except TelegramError as e:
            logger.error(f"Bot cannot access feedback channel: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error checking channel access: {e}")
            return False
    
    async def test_channel_access(self, context: ContextTypes.DEFAULT_TYPE):
        """Test if bot can send a message to the feedback channel"""
        try:
            # Test text message first
            test_message = await context.bot.send_message(
                chat_id=FEEDBACK_CHANNEL_ID,
                text="🔧 **Bot Test Message**\n\nThis is a test message to verify bot access to the feedback channel.",
                parse_mode='Markdown'
            )
            # Delete the test message immediately
            await context.bot.delete_message(
                chat_id=FEEDBACK_CHANNEL_ID,
                message_id=test_message.message_id
            )
            logger.info("Successfully sent and deleted test message to feedback channel")
            
            # Test photo sending capability (if PIL is available)
            if PIL_AVAILABLE:
                try:
                    # Create a simple test image
                    img = Image.new('RGB', (200, 100), color = 'blue')
                    d = ImageDraw.Draw(img)
                    d.text((10,10), "Test Image", fill=(255,255,0))
                    
                    # Convert to bytes
                    img_bytes = io.BytesIO()
                    img.save(img_bytes, format='JPEG')
                    img_bytes.seek(0)
                    
                    # Send test photo
                    test_photo = await context.bot.send_photo(
                        chat_id=FEEDBACK_CHANNEL_ID,
                        photo=img_bytes,
                        caption="Test photo - will be deleted"
                    )
                    # Delete the test photo immediately
                    await context.bot.delete_message(
                        chat_id=FEEDBACK_CHANNEL_ID,
                        message_id=test_photo.message_id
                    )
                    logger.info("Successfully sent and deleted test photo to feedback channel")
                except Exception as e:
                    logger.warning(f"Could not test photo sending: {e}")
                    # Don't fail the test if photo sending fails, just log it
            
            return True
        except Exception as e:
            logger.error(f"Failed to send test message to feedback channel: {e}")
            return False
    
    def setup_handlers(self):
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("generate", self.generate_command))
        self.application.add_handler(CommandHandler("history", self.history_command))
        self.application.add_handler(CommandHandler("view", self.view_command))
        self.application.add_handler(CommandHandler("delete", self.delete_command))
        self.application.add_handler(CommandHandler("ban", self.ban_command))
        self.application.add_handler(CommandHandler("unban", self.unban_command))
        self.application.add_handler(CommandHandler("broadcast", self.broadcast_command))
        self.application.add_handler(CommandHandler("stats", self.stats_command))
        self.application.add_handler(CommandHandler("feedback", self.feedback_command))
        self.application.add_handler(CommandHandler("cancel", self.cancel_command))
        self.application.add_handler(CommandHandler("otp", self.otp_command))
        self.application.add_handler(CallbackQueryHandler(self.button_callback))
        self.application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, self.handle_feedback_photo))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_message))
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if self.db.is_user_banned(user_id):
            await update.message.reply_text("❌ You are banned from using this bot.")
            return
        
        self.db.add_user(user_id)
        
        welcome_text = """
🤖 **Welcome to TempGail Bot!**

📧 **Generate unique temporary Gmail aliases**

**Available Commands:**
/generate - Create a new random alias
/generate [custom] - Create custom alias (e.g., /generate myalias)
/history - View your alias history
/view [alias] - Switch to view messages for specific alias
/delete [alias] - Delete an alias
/otp - View recent OTP codes and verification links
/feedback - Send feedback to the admin

**Features:**
• 🎲 Unique alias generation with 10+ formats
• 🕒 Messages auto-expire after 1 hour
• 🔍 OTP codes automatically detected
• 🔗 Verification links automatically extracted
• 📋 One-click copy buttons
• 👥 Multi-user support
• 🔔 Instant OTP notifications

Click /generate to get started! 🚀
        """
        
        keyboard = [
            [InlineKeyboardButton("🎲 Generate Random Alias", callback_data="generate_random")],
            [InlineKeyboardButton("🔑 View OTPs", callback_data="view_otp")],
            [InlineKeyboardButton("💬 Send Feedback", callback_data="send_feedback")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode='Markdown')
    
    async def cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if user_id in self.feedback_users:
            self.feedback_users.discard(user_id)
            # Clean up user data
            if user_id in self.user_data:
                del self.user_data[user_id]
            await update.message.reply_text("✅ Feedback mode cancelled. You can now use the bot normally.")
        else:
            await update.message.reply_text("❌ You're not in feedback mode.")
    
    async def otp_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show recent OTP codes and verification links"""
        user_id = update.effective_user.id
        
        if self.db.is_user_banned(user_id):
            await update.message.reply_text("❌ You are banned from using this bot.")
            return
        
        # Get recent messages with OTP or links
        messages = self.db.get_recent_messages(user_id)
        
        # Filter messages with OTP or links
        otp_messages = []
        for msg in messages:
            msg_id, alias, subject, body, received_at, seen, otp_code, verification_links = msg
            
            # Parse verification links from JSON
            links = json.loads(verification_links) if verification_links else []
            
            if otp_code or links:
                otp_messages.append({
                    'id': msg_id,
                    'alias': alias,
                    'subject': subject,
                    'received_at': received_at,
                    'otp': otp_code,
                    'links': links
                })
        
        if not otp_messages:
            await update.message.reply_text("📭 No OTP codes or verification links found recently.")
            return
        
        # Display OTP messages
        text = "🔑 **Recent OTP Codes & Verification Links**\n\n"
        keyboard = []
        
        for msg in otp_messages[:5]:  # Show last 5
            time_str = datetime.strptime(msg['received_at'], '%Y-%m-%d %H:%M:%S').strftime('%H:%M')
            
            text += f"📧 **{msg['alias']}** - {time_str}\n"
            
            if msg['otp']:
                text += f"🔑 **OTP:** `{msg['otp']}`\n"
                # Store OTP for copy button
                if user_id not in self.user_data:
                    self.user_data[user_id] = {}
                self.user_data[user_id][f'otp_{msg["id"]}'] = msg['otp']
            
            if msg['links']:
                text += f"🔗 **Links:**\n"
                for i, link in enumerate(msg['links'][:2]):  # Show max 2 links
                    text += f"  • [Link {i+1}]({link[:50]}...)\n"
                    # Store link for copy button
                    if user_id not in self.user_data:
                        self.user_data[user_id] = {}
                    self.user_data[user_id][f'link_{msg["id"]}_{i}'] = link
            
            text += "\n"
            
            # Add copy buttons
            row = []
            if msg['otp']:
                row.append(InlineKeyboardButton("📋 Copy OTP", callback_data=f"copy_otp_{msg['id']}"))
            if msg['links']:
                row.append(InlineKeyboardButton("🔗 Copy Link", callback_data=f"copy_link_{msg['id']}_0"))
            if row:
                keyboard.append(row)
        
        keyboard.append([
            InlineKeyboardButton("🔄 Refresh", callback_data="view_otp"),
            InlineKeyboardButton("💬 Send Feedback", callback_data="send_feedback")
        ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    
    async def feedback_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if self.db.is_user_banned(user_id):
            await update.message.reply_text("❌ You are banned from using this bot.")
            return
        
        # Check channel access and permissions
        if not await self.check_channel_access(context):
            await update.message.reply_text("❌ Feedback system is currently unavailable. Please try again later.")
            return
        
        if not await self.check_channel_permissions(context):
            await update.message.reply_text("❌ Bot doesn't have permission to send messages in the feedback channel. Please contact the admin.")
            return
        
        # Test if bot can actually send a message to the channel
        if not await self.test_channel_access(context):
            await update.message.reply_text("❌ Feedback system is currently unavailable. Please try again later.")
            return
        
        self.feedback_users.add(user_id)
        
        feedback_text = """
💬 **Send Your Feedback**

Please share your thoughts, suggestions, or report issues with the bot. You can:

• Send a text message with your feedback
• Send a photo with a caption (optional)

Your feedback helps us improve the bot! Thank you for your support.

Type /cancel to exit feedback mode.
        """
        
        await update.message.reply_text(feedback_text, parse_mode='Markdown')
    
    async def handle_feedback_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if user_id not in self.feedback_users:
            return  # Not in feedback mode
        
        # Get user information
        user = update.effective_user
        user_info = f"👤 **User Information:**\n"
        user_info += f"👤 Name: {user.first_name} {user.last_name or ''}\n"
        user_info += f"🔗 Username: @{user.username or 'N/A'}\n"
        user_info += f"🆔 User ID: {user.id}\n"
        
        # Get caption if provided
        caption = update.message.caption or "No caption provided"
        
        # Get the best quality photo
        photo = update.message.photo[-1]
        photo_file = await photo.get_file()
        
        # Check file size
        try:
            file_size = photo.file_size
            logger.info(f"Photo file size: {file_size} bytes")
            if file_size > 20 * 1024 * 1024:  # 20MB limit
                await update.message.reply_text("❌ Photo is too large. Please send a photo smaller than 20MB.")
                self.feedback_users.discard(user_id)
                return
        except:
            logger.warning("Could not determine photo file size")
        
        # Save feedback to database
        try:
            feedback_id = self.db.save_feedback(user_id, feedback_text=caption, feedback_photo_id=photo_file.file_id)
            logger.info(f"Saved feedback with ID: {feedback_id}")
        except Exception as e:
            logger.error(f"Failed to save feedback to database: {e}")
            await update.message.reply_text("❌ Failed to save feedback. Please try again later.")
            self.feedback_users.discard(user_id)
            return
        
        success = False
        error_details = []
        
        # Method 1: Send photo directly using file_id
        try:
            sent_message = await context.bot.send_photo(
                chat_id=FEEDBACK_CHANNEL_ID,
                photo=photo_file.file_id,
                caption=f"{user_info}\n\n**Feedback ID:** {feedback_id}\n\n**Caption:** {caption}",
                parse_mode='Markdown'
            )
            success = True
            logger.info(f"Photo sent successfully using file_id. Message ID: {sent_message.message_id}")
        except Exception as e:
            error_details.append(f"File ID method: {str(e)}")
            logger.error(f"Failed to send photo with file_id: {e}")
        
        # Method 2: Download and re-upload if first method failed
        if not success:
            try:
                logger.info("Attempting to download and re-upload photo...")
                photo_bytes = await photo_file.download_as_bytearray()
                
                # Create a file-like object
                photo_io = io.BytesIO(photo_bytes)
                photo_io.name = f"feedback_{feedback_id}.jpg"
                
                # Send the downloaded photo
                sent_message = await context.bot.send_photo(
                    chat_id=FEEDBACK_CHANNEL_ID,
                    photo=photo_io,
                    caption=f"{user_info}\n\n**Feedback ID:** {feedback_id}\n\n**Caption:** {caption}",
                    parse_mode='Markdown'
                )
                success = True
                logger.info(f"Photo sent successfully after download and re-upload. Message ID: {sent_message.message_id}")
            except Exception as e:
                error_details.append(f"Download and re-upload: {str(e)}")
                logger.error(f"Failed to send photo after download: {e}")
        
        # Method 3: Try smaller photo versions if still failed
        if not success and len(update.message.photo) > 1:
            for i in range(len(update.message.photo) - 1, 0, -1):  # Try from largest to smallest
                try:
                    smaller_photo = update.message.photo[i-1]
                    smaller_file = await smaller_photo.get_file()
                    sent_message = await context.bot.send_photo(
                        chat_id=FEEDBACK_CHANNEL_ID,
                        photo=smaller_file.file_id,
                        caption=f"{user_info}\n\n**Feedback ID:** {feedback_id}\n\n**Caption:** {caption}\n\n(Smaller version)",
                        parse_mode='Markdown'
                    )
                    success = True
                    logger.info(f"Photo sent successfully using smaller version (index {i-1}). Message ID: {sent_message.message_id}")
                    break
                except Exception as e:
                    error_details.append(f"Smaller version {i-1}: {str(e)}")
                    logger.error(f"Failed to send smaller version {i-1}: {e}")
        
        # Method 4: Send as document if all photo methods failed
        if not success:
            try:
                logger.info("Attempting to send photo as document...")
                photo_bytes = await photo_file.download_as_bytearray()
                
                # Create a file-like object
                photo_io = io.BytesIO(photo_bytes)
                photo_io.name = f"feedback_{feedback_id}.jpg"
                
                # Send as document
                sent_message = await context.bot.send_document(
                    chat_id=FEEDBACK_CHANNEL_ID,
                    document=photo_io,
                    caption=f"{user_info}\n\n**Feedback ID:** {feedback_id}\n\n**Photo Feedback (sent as document):**\n{caption}",
                    parse_mode='Markdown'
                )
                success = True
                logger.info(f"Photo sent successfully as document. Message ID: {sent_message.message_id}")
            except Exception as e:
                error_details.append(f"Send as document: {str(e)}")
                logger.error(f"Failed to send photo as document: {e}")
        
        # Method 5: Try sending without markdown if all methods failed
        if not success:
            try:
                logger.info("Attempting to send photo without markdown...")
                photo_bytes = await photo_file.download_as_bytearray()
                
                # Create a file-like object
                photo_io = io.BytesIO(photo_bytes)
                photo_io.name = f"feedback_{feedback_id}.jpg"
                
                # Send without markdown
                sent_message = await context.bot.send_photo(
                    chat_id=FEEDBACK_CHANNEL_ID,
                    photo=photo_io,
                    caption=f"User Information:\nName: {user.first_name} {user.last_name or ''}\nUsername: @{user.username or 'N/A'}\nUser ID: {user.id}\n\nFeedback ID: {feedback_id}\n\nCaption: {caption}"
                )
                success = True
                logger.info(f"Photo sent successfully without markdown. Message ID: {sent_message.message_id}")
            except Exception as e:
                error_details.append(f"Send without markdown: {str(e)}")
                logger.error(f"Failed to send photo without markdown: {e}")
        
        # Final fallback: Send text with photo info if all methods failed
        if not success:
            try:
                error_text = "\n".join(error_details[:3])  # Limit error details
                sent_message = await context.bot.send_message(
                    chat_id=FEEDBACK_CHANNEL_ID,
                    text=f"{user_info}\n\n**Feedback ID:** {feedback_id}\n\n❌ **Photo Failed to Send**\n\n**Caption:** {caption}\n\n**Photo File ID:** `{photo_file.file_id}`\n\n**Errors:**\n{error_text}",
                    parse_mode='Markdown'
                )
                success = True
                logger.info(f"Sent text fallback with detailed error info. Message ID: {sent_message.message_id}")
            except Exception as e:
                logger.error(f"Failed to send text fallback: {e}")
        
        # Send confirmation to user
        if success:
            await update.message.reply_text("✅ Thank you for your feedback! We appreciate your input.")
        else:
            await update.message.reply_text("❌ Failed to send feedback. Please try again later.")
        
        # Remove user from feedback mode
        self.feedback_users.discard(user_id)
    
    async def handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        text = update.message.text
        
        # Check if user is in feedback mode
        if user_id in self.feedback_users:
            # Get user information
            user = update.effective_user
            user_info = f"👤 **User Information:**\n"
            user_info += f"👤 Name: {user.first_name} {user.last_name or ''}\n"
            user_info += f"🔗 Username: @{user.username or 'N/A'}\n"
            user_info += f"🆔 User ID: {user.id}\n"
            
            # Save feedback to database
            try:
                feedback_id = self.db.save_feedback(user_id, feedback_text=text)
                logger.info(f"Saved text feedback with ID: {feedback_id}")
            except Exception as e:
                logger.error(f"Failed to save text feedback to database: {e}")
                await update.message.reply_text("❌ Failed to save feedback. Please try again later.")
                self.feedback_users.discard(user_id)
                return
            
            success = False
            
            # Send to feedback channel
            try:
                await context.bot.send_message(
                    chat_id=FEEDBACK_CHANNEL_ID,
                    text=f"{user_info}\n\n**Feedback ID:** {feedback_id}\n\n**Feedback:**\n{text}",
                    parse_mode='Markdown'
                )
                success = True
                logger.info("Text feedback sent successfully with markdown")
            except Exception as e:
                logger.error(f"Failed to send text feedback with markdown: {e}")
                # Try sending without markdown
                try:
                    await context.bot.send_message(
                        chat_id=FEEDBACK_CHANNEL_ID,
                        text=f"{user_info}\n\nFeedback ID: {feedback_id}\n\nFeedback:\n{text}"
                    )
                    success = True
                    logger.info("Text feedback sent successfully without markdown")
                except Exception as e2:
                    logger.error(f"Failed to send text feedback without markdown: {e2}")
            
            # Send confirmation to user
            if success:
                await update.message.reply_text("✅ Thank you for your feedback! We appreciate your input.")
            else:
                await update.message.reply_text("❌ Failed to send feedback. Please try again later.")
            
            # Remove user from feedback mode
            self.feedback_users.discard(user_id)
            return
        
        # Check if it's a custom alias
        if re.match(r'^[a-zA-Z0-9_-]{1,20}$', text):
            context.args = [text]
            await self.generate_command(update, context)
        else:
            await update.message.reply_text("❌ Invalid alias format. Use only letters, numbers, hyphens, and underscores (max 20 chars).")
    
    async def generate_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if self.db.is_user_banned(user_id):
            await update.message.reply_text("❌ You are banned from using this bot.")
            return
        
        custom_alias = context.args[0] if context.args else None
        
        if custom_alias:
            if not re.match(r'^[a-zA-Z0-9_-]{1,20}$', custom_alias):
                await update.message.reply_text("❌ Invalid alias format. Use only letters, numbers, hyphens, and underscores (max 20 chars).")
                return
            
            alias_name = custom_alias.lower()
        else:
            # Use the new AliasGenerator
            alias_name = AliasGenerator.generate_random_alias()
        
        full_alias = f"9411revop2+{alias_name}@gmail.com"
        
        if self.db.add_alias(user_id, alias_name):
            # Determine the format type for display
            format_type = self._detect_alias_format(alias_name)
            
            keyboard = [
                [InlineKeyboardButton("👀 View Messages", callback_data=f"view_{alias_name}")],
                [InlineKeyboardButton("🔑 View OTPs", callback_data="view_otp")],
                [InlineKeyboardButton("💬 Send Feedback", callback_data="send_feedback")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"✅ **New alias created!**\n\n"
                f"📧 `{full_alias}`\n\n"
                f"🎨 **Format:** {format_type}\n"
                f"• Messages will appear here automatically\n"
                f"• OTP codes will be detected automatically\n"
                f"• Verification links will be extracted\n"
                f"• Expires after 1 hour of inactivity\n"
                f"• Use /otp to view all OTPs",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text("❌ Alias already exists. Try a different name.")
    
    def _detect_alias_format(self, alias):
        """Detect the format type of an alias for display"""
        if '_' in alias:
            return "Snake Case"
        elif any(c.isupper() for c in alias):
            return "Camel Case"
        elif re.match(r'^[a-f0-9]+$', alias):
            return "Hexadecimal"
        elif '-' in alias:
            return "Timestamp Style"
        elif re.search(r'[4e1i0st]', alias) and any(c.isdigit() for c in alias):
            return "Leet Style"
        elif alias.isalpha() and any(c.isdigit() for c in alias):
            return "Word + Number"
        elif alias.isalpha():
            return "Pronounceable"
        elif len(set(alias)) < len(alias) * 0.5:
            return "Mixed Characters"
        else:
            return "UUID Style"
    
    async def history_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if self.db.is_user_banned(user_id):
            await update.message.reply_text("❌ You are banned from using this bot.")
            return
        
        aliases = self.db.get_user_aliases(user_id, limit=10)
        
        if not aliases:
            await update.message.reply_text("📭 No aliases found. Use /generate to create one.")
            return
        
        text = "📋 **Your Aliases**\n\n"
        keyboard = []
        
        for alias_name, created_at, active in aliases:
            status = "✅" if active else "❌"
            full_alias = f"9411revop2+{alias_name}@gmail.com"
            format_type = self._detect_alias_format(alias_name)
            text += f"{status} `{full_alias}` ({format_type})\n"
            
            row = []
            if active:
                row.append(InlineKeyboardButton("👀 View", callback_data=f"view_{alias_name}"))
                row.append(InlineKeyboardButton("❌ Delete", callback_data=f"del_{alias_name}"))
            else:
                row.append(InlineKeyboardButton("✅ Restore", callback_data=f"res_{alias_name}"))
            keyboard.append(row)
        
        keyboard.append([
            InlineKeyboardButton("🎲 New Random Alias", callback_data="generate_random"),
            InlineKeyboardButton("🔑 View OTPs", callback_data="view_otp"),
            InlineKeyboardButton("💬 Send Feedback", callback_data="send_feedback")
        ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    
    async def view_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if self.db.is_user_banned(user_id):
            await update.message.reply_text("❌ You are banned from using this bot.")
            return
        
        if not context.args:
            await update.message.reply_text("❌ Usage: /view <alias_name>")
            return
        
        alias_name = context.args[0].lower()
        message = await update.message.reply_text("Loading messages: ▱▱▱▱▱▱▱ 0.00 %")
        await self.show_loading_animation(message)
        await self._show_alias_messages(update, user_id, alias_name, message=message)
    
    async def delete_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if self.db.is_user_banned(user_id):
            await update.message.reply_text("❌ You are banned from using this bot.")
            return
        
        if not context.args:
            await update.message.reply_text("❌ Usage: /delete <alias_name>")
            return
        
        alias_name = context.args[0].lower()
        if self.db.set_alias_active(user_id, alias_name, False):
            await update.message.reply_text(f"✅ Alias `{alias_name}` deleted. Messages will no longer be shown.", parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Alias not found or you don't have permission to delete it.")
    
    async def ban_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("❌ Admin only command.")
            return
        
        if not context.args:
            await update.message.reply_text("❌ Usage: /ban <user_id>")
            return
        
        try:
            target_user_id = int(context.args[0])
            self.db.ban_user(target_user_id)
            await update.message.reply_text(f"✅ User {target_user_id} banned.")
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID.")
    
    async def unban_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("❌ Admin only command.")
            return
        
        if not context.args:
            await update.message.reply_text("❌ Usage: /unban <user_id>")
            return
        
        try:
            target_user_id = int(context.args[0])
            self.db.unban_user(target_user_id)
            await update.message.reply_text(f"✅ User {target_user_id} unbanned.")
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID.")
    
    async def broadcast_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("❌ Admin only command.")
            return
        
        if not context.args:
            await update.message.reply_text("❌ Usage: /broadcast <message>")
            return
        
        broadcast_message = ' '.join(context.args)
        user_ids = self.db.get_all_user_ids()
        
        sent_count = 0
        failed_count = 0
        
        for target_id in user_ids:
            try:
                await context.bot.send_message(chat_id=target_id, text=broadcast_message, parse_mode='Markdown')
                sent_count += 1
            except Exception as e:
                logger.error(f"Failed to send broadcast to {target_id}: {e}")
                failed_count += 1
        
        await update.message.reply_text(
            f"📢 Broadcast completed!\n"
            f"✅ Sent to {sent_count} users\n"
            f"❌ Failed for {failed_count} users"
        )
    
    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("❌ Admin only command.")
            return
        
        total_users = self.db.get_user_count()
        banned_users = self.db.get_banned_user_count()
        total_aliases = self.db.get_alias_count()
        active_aliases = self.db.get_active_alias_count()
        total_messages = self.db.get_message_count()
        
        stats_text = (
            "📊 **Bot Statistics**\n\n"
            f"👥 Total Users: {total_users}\n"
            f"🚫 Banned Users: {banned_users}\n"
            f"📧 Total Aliases: {total_aliases}\n"
            f"✅ Active Aliases: {active_aliases}\n"
            f"💬 Total Messages: {total_messages}"
        )
        
        await update.message.reply_text(stats_text, parse_mode='Markdown')
    
    async def show_loading_animation(self, message):
        steps = 7
        for i in range(1, steps + 1):
            bar = ''.join(['▰' if j < i else '▱' for j in range(steps)])
            percent = f"{(i / steps * 100):.2f} %"
            text = f"Loading messages: {bar} {percent}"
            try:
                await message.edit_text(text)
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.error(f"Failed to update loading animation: {e}")
                break
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        data = query.data
        
        if self.db.is_user_banned(user_id):
            await query.edit_message_text("❌ You are banned from using this bot.")
            return
        
        # Handle quick copy OTP callback
        if data.startswith("quick_copy_otp_"):
            otp = data.split("_", 3)[3]
            try:
                await query.message.reply_text(f"📋 OTP copied: `{otp}`", parse_mode='Markdown')
            except:
                await query.message.reply_text(f"📋 OTP copied: {otp}")
            return
        
        # Handle quick copy link callback
        if data.startswith("quick_copy_link_"):
            link = data.split("_", 3)[3]
            try:
                await query.message.reply_text(f"🔗 Link copied: `{link}`", parse_mode='Markdown')
            except:
                await query.message.reply_text(f"🔗 Link copied: {link}")
            return
        
        # Handle copy OTP callback
        if data.startswith("copy_otp_"):
            msg_id = data.split("_", 2)[2]
            if user_id in self.user_data and f'otp_{msg_id}' in self.user_data[user_id]:
                otp = self.user_data[user_id][f'otp_{msg_id}']
                try:
                    await query.message.reply_text(f"📋 OTP copied: `{otp}`", parse_mode='Markdown')
                except:
                    await query.message.reply_text(f"📋 OTP copied: {otp}")
            return
        
        # Handle copy link callback
        if data.startswith("copy_link_"):
            parts = data.split("_", 3)
            msg_id = parts[2]
            link_index = parts[3]
            key = f'link_{msg_id}_{link_index}'
            if user_id in self.user_data and key in self.user_data[user_id]:
                link = self.user_data[user_id][key]
                try:
                    await query.message.reply_text(f"🔗 Link copied: `{link}`", parse_mode='Markdown')
                except:
                    await query.message.reply_text(f"🔗 Link copied: {link}")
            return
        
        if data == "generate_random":
            # Use the new AliasGenerator
            alias_name = AliasGenerator.generate_random_alias()
            full_alias = f"9411revop2+{alias_name}@gmail.com"
            
            if self.db.add_alias(user_id, alias_name):
                # Determine the format type
                format_type = self._detect_alias_format(alias_name)
                
                keyboard = [
                    [InlineKeyboardButton("👀 View Messages", callback_data=f"view_{alias_name}")],
                    [InlineKeyboardButton("🔑 View OTPs", callback_data="view_otp")],
                    [InlineKeyboardButton("💬 Send Feedback", callback_data="send_feedback")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await query.edit_message_text(
                    f"✅ **New alias created!**\n\n"
                    f"📧 `{full_alias}`\n\n"
                    f"🎨 **Format:** {format_type}\n"
                    f"• Messages will appear here automatically\n"
                    f"• OTP codes will be detected automatically\n"
                    f"• Verification links will be extracted\n"
                    f"• Expires after 1 hour of inactivity",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
        
        elif data == "view_otp":
            # Get recent messages with OTP or links
            messages = self.db.get_recent_messages(user_id)
            
            # Filter messages with OTP or links
            otp_messages = []
            for msg in messages:
                msg_id, alias, subject, body, received_at, seen, otp_code, verification_links = msg
                
                # Parse verification links from JSON
                links = json.loads(verification_links) if verification_links else []
                
                if otp_code or links:
                    otp_messages.append({
                        'id': msg_id,
                        'alias': alias,
                        'subject': subject,
                        'received_at': received_at,
                        'otp': otp_code,
                        'links': links
                    })
            
            if not otp_messages:
                await query.edit_message_text("📭 No OTP codes or verification links found recently.")
                return
            
            # Display OTP messages
            text = "🔑 **Recent OTP Codes & Verification Links**\n\n"
            keyboard = []
            
            for msg in otp_messages[:5]:  # Show last 5
                time_str = datetime.strptime(msg['received_at'], '%Y-%m-%d %H:%M:%S').strftime('%H:%M')
                
                text += f"📧 **{msg['alias']}** - {time_str}\n"
                
                if msg['otp']:
                    text += f"🔑 **OTP:** `{msg['otp']}`\n"
                    # Store OTP for copy button
                    if user_id not in self.user_data:
                        self.user_data[user_id] = {}
                    self.user_data[user_id][f'otp_{msg["id"]}'] = msg['otp']
                
                if msg['links']:
                    text += f"🔗 **Links:**\n"
                    for i, link in enumerate(msg['links'][:2]):  # Show max 2 links
                        text += f"  • [Link {i+1}]({link[:50]}...)\n"
                        # Store link for copy button
                        if user_id not in self.user_data:
                            self.user_data[user_id] = {}
                        self.user_data[user_id][f'link_{msg["id"]}_{i}'] = link
                
                text += "\n"
                
                # Add copy buttons
                row = []
                if msg['otp']:
                    row.append(InlineKeyboardButton("📋 Copy OTP", callback_data=f"copy_otp_{msg['id']}"))
                if msg['links']:
                    row.append(InlineKeyboardButton("🔗 Copy Link", callback_data=f"copy_link_{msg['id']}_0"))
                if row:
                    keyboard.append(row)
            
            keyboard.append([
                InlineKeyboardButton("🔄 Refresh", callback_data="view_otp"),
                InlineKeyboardButton("💬 Send Feedback", callback_data="send_feedback")
            ])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
        
        elif data == "send_feedback":
            # Check channel access and permissions
            if not await self.check_channel_access(context):
                await query.edit_message_text("❌ Feedback system is currently unavailable. Please try again later.")
                return
            
            if not await self.check_channel_permissions(context):
                await query.edit_message_text("❌ Bot doesn't have permission to send messages in the feedback channel. Please contact the admin.")
                return
            
            # Test if bot can actually send a message to the channel
            if not await self.test_channel_access(context):
                await query.edit_message_text("❌ Feedback system is currently unavailable. Please try again later.")
                return
            
            self.feedback_users.add(user_id)
            
            feedback_text = """
💬 **Send Your Feedback**

Please share your thoughts, suggestions, or report issues with the bot. You can:

• Send a text message with your feedback
• Send a photo with a caption (optional)

Your feedback helps us improve the bot! Thank you for your support.

Type /cancel to exit feedback mode.
            """
            
            await query.edit_message_text(feedback_text, parse_mode='Markdown')
        
        elif data.startswith("view_"):
            alias_name = data.split("_", 1)[1].lower()
            await self.show_loading_animation(query.message)
            await self._show_alias_messages(update, user_id, alias_name, query=query)
        
        elif data.startswith("del_"):
            alias_name = data.split("_", 1)[1].lower()
            if self.db.set_alias_active(user_id, alias_name, False):
                await query.edit_message_text(f"✅ Alias `{alias_name}` deleted.", parse_mode='Markdown')
            else:
                await query.answer("❌ Failed to delete alias")
        
        elif data.startswith("res_"):
            alias_name = data.split("_", 1)[1].lower()
            if self.db.set_alias_active(user_id, alias_name, True):
                await query.edit_message_text(f"✅ Alias `{alias_name}` restored.", parse_mode='Markdown')
            else:
                await query.answer("❌ Failed to restore alias")
    
    async def _show_alias_messages(self, update: Update, user_id: int, alias_name: str, query=None, message=None):
        alias_name = alias_name.lower()
        cursor = self.db.conn.cursor()
        cursor.execute('SELECT alias_id FROM aliases WHERE user_id = ? AND alias_name = ? AND active = TRUE', 
                       (user_id, alias_name))
        if not cursor.fetchone():
            text = f"❌ Alias `{alias_name}` not found or inactive. Use /history to see available aliases."
            logger.warning(f"Alias {alias_name} not found or inactive for user {user_id}")
            if query:
                await query.edit_message_text(text, parse_mode='Markdown')
            elif message:
                await message.edit_text(text, parse_mode='Markdown')
            else:
                await update.message.reply_text(text, parse_mode='Markdown')
            return
        
        messages = self.db.get_recent_messages(user_id)
        alias_messages = [msg for msg in messages if msg[1].lower() == alias_name]
        
        logger.info(f"Found {len(alias_messages)} messages for alias {alias_name} and user {user_id}")
        
        if not alias_messages:
            text = f"📭 No messages found for `{alias_name}`.\n\nNew messages will appear here automatically."
            keyboard = [
                [InlineKeyboardButton("🔄 Refresh", callback_data=f"view_{alias_name}")],
                [InlineKeyboardButton("🔑 View OTPs", callback_data="view_otp")],
                [InlineKeyboardButton("💬 Send Feedback", callback_data="send_feedback")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            if query:
                await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
            elif message:
                await message.edit_text(text, reply_markup=reply_markup, parse_mode='Markdown')
            else:
                await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')
            return
        
        # Display messages
        text = f"📧 **Messages for `{alias_name}`**\n\n"
        keyboard = []
        
        for msg in alias_messages[:5]:  # Show last 5 messages
            msg_id, alias, subject, body, received_at, seen, otp_code, verification_links = msg
            time_str = datetime.strptime(received_at, '%Y-%m-%d %H:%M:%S').strftime('%H:%M')
            
            # Parse verification links from JSON
            links = json.loads(verification_links) if verification_links else []
            
            # Extract OTP if not already extracted
            if not otp_code:
                otp_code = self.otp_service.extract_otp(body)
            
            # Extract links if not already extracted
            if not links:
                links = self.otp_service.extract_verification_links(body)
            
            # Display OTP if found
            otp_text = f"\n🔑 **OTP:** `{otp_code}`" if otp_code else ""
            
            # Display links if found
            links_text = ""
            if links:
                links_text = "\n🔗 **Links:**"
                for i, link in enumerate(links[:3]):  # Show max 3 links
                    links_text += f"\n[Link {i+1}]({link})"
            
            # Truncate body
            body_preview = body[:200] + "..." if len(body) > 200 else body
            body_preview = body_preview.replace('\n', ' ').replace('\r', '')
            
            text += f"📨 **{subject or 'No Subject'}** - {time_str}{otp_text}\n"
            text += f"```{body_preview}```{links_text}\n\n"
            
            # Mark as seen
            self.db.mark_message_seen(msg_id)
            
            # Add copy buttons
            row = []
            if otp_code:
                # Store OTP in user_data
                if user_id not in self.user_data:
                    self.user_data[user_id] = {}
                self.user_data[user_id][f'otp_{msg_id}'] = otp_code
                row.append(InlineKeyboardButton(f"📋 Copy OTP", callback_data=f"copy_otp_{msg_id}"))
            if links:
                # Store first link in user_data
                if user_id not in self.user_data:
                    self.user_data[user_id] = {}
                self.user_data[user_id][f'link_{msg_id}_0'] = links[0]
                row.append(InlineKeyboardButton(f"🔗 Copy Link", callback_data=f"copy_link_{msg_id}_0"))
            if row:
                keyboard.append(row)
        
        keyboard.append([
            InlineKeyboardButton("🔄 Refresh", callback_data=f"view_{alias_name}"),
            InlineKeyboardButton("🔑 View OTPs", callback_data="view_otp"),
            InlineKeyboardButton("💬 Send Feedback", callback_data="send_feedback")
        ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if query:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
        elif message:
            await message.edit_text(text, reply_markup=reply_markup, parse_mode='Markdown')
        else:
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    
    async def process_notifications_periodically(self):
        """Process notifications periodically"""
        while True:
            try:
                await notification_queue.process_notifications()
                await asyncio.sleep(1)  # Check every second
            except Exception as e:
                logger.error(f"Error in notification processing: {e}")
                await asyncio.sleep(5)
    
    async def run(self):
        self.application = Application.builder().token(BOT_TOKEN).build()
        
        # Set the bot application in notification queue
        notification_queue.set_bot(self.application)
        
        self.setup_handlers()
        
        # Start Gmail polling
        self.gmail.start_polling()
        
        # Start notification processing task
        notification_task = asyncio.create_task(self.process_notifications_periodically())
        
        # Start bot
        logger.info("Starting bot...")
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling(drop_pending_updates=True)
        
        # Keep running
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            self.gmail.stop_polling()
            notification_task.cancel()
            await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()

# Plesk-compatible entry point
def handler(event):
    """Plesk handler function"""
    try:
        # This is the entry point for Plesk
        bot = TelegramBot()
        asyncio.run(bot.run())
    except Exception as e:
        logger.error(f"Error in handler: {e}")
        return {"statusCode": 500, "body": str(e)}
    
    return {"statusCode": 200, "body": "Bot started successfully"}

# Main execution
if __name__ == '__main__':
    bot = TelegramBot()
    asyncio.run(bot.run())