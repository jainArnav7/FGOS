# ============================================================
# BATTLESHIP GAME — Separate module for clean organization
# ============================================================

import discord
import random
import asyncio
from typing import Optional, Tuple


class Ship:
    """Represents a single ship on the board"""
    def __init__(self, name: str, size: int):
        self.name = name
        self.size = size
        self.positions = []  # [(row, col), ...]
        self.hits = set()    # {(row, col), ...}
    
    def place(self, start_row: int, start_col: int, horizontal: bool):
        """Place ship at coordinates"""
        self.positions = []
        for i in range(self.size):
            if horizontal:
                self.positions.append((start_row, start_col + i))
            else:
                self.positions.append((start_row + i, start_col))
    
    def hit(self, row: int, col: int) -> bool:
        """Mark a hit if this position contains the ship"""
        if (row, col) in self.positions:
            self.hits.add((row, col))
            return True
        return False
    
    def is_sunk(self) -> bool:
        """Check if all parts are hit"""
        return len(self.hits) == self.size
    
    def get_health(self) -> str:
        """Visual health bar"""
        hits = len(self.hits)
        total = self.size
        filled = "🔴" * hits
        empty = "⚪" * (total - hits)
        return f"{filled}{empty}"


class BattleshipBoard:
    """Represents one player's board"""
    def __init__(self):
        self.size = 10
        self.ships = []
        self.hits = set()      # {(row, col)} - successful shots
        self.misses = set()    # {(row, col)} - missed shots
    
    def add_ship(self, ship: Ship):
        """Add a ship to the board"""
        self.ships.append(ship)
    
    def shoot(self, row: int, col: int) -> Tuple[str, Optional[Ship]]:
        """
        Fire at a coordinate.
        Returns: ('hit', ship), ('miss', None), or ('already_shot', None)
        """
        if (row, col) in self.hits or (row, col) in self.misses:
            return ('already_shot', None)
        
        # Check if any ship is hit
        for ship in self.ships:
            if ship.hit(row, col):
                self.hits.add((row, col))
                return ('hit', ship)
        
        # Miss
        self.misses.add((row, col))
        return ('miss', None)
    
    def get_cell(self, row: int, col: int, reveal_ships: bool = False) -> str:
        """Get emoji for a board cell"""
        if (row, col) in self.hits:
            return "💥"  # Direct hit
        if (row, col) in self.misses:
            return "💧"  # Miss/water
        if reveal_ships:
            for ship in self.ships:
                if (row, col) in ship.positions:
                    return "🚢"  # Your ship
        return "⬜"  # Empty/unknown
    
    def all_sunk(self) -> bool:
        """Check if all ships are destroyed"""
        return all(ship.is_sunk() for ship in self.ships)
    
    def count_sunk(self) -> int:
        """How many ships are sunk"""
        return sum(1 for ship in self.ships if ship.is_sunk())


class BattleshipGame:
    """Main game logic and state"""
    
    # Standard battleship fleet
    SHIPS = [
        ("🚢 Battleship", 4),
        ("⛴️  Cruiser 1", 3),
        ("⛴️  Cruiser 2", 3),
        ("🛥️  Submarine", 2),
        ("⚓ Destroyer", 1),
    ]
    
    def __init__(self, player1_id: int, player1_name: str, 
                 player2_id: Optional[int] = None, player2_name: Optional[str] = None, 
                 is_ai: bool = False, channel: discord.TextChannel = None):
        self.game_id = None
        self.channel = channel
        
        # Players
        self.player1_id = player1_id
        self.player1_name = player1_name
        self.player2_id = player2_id or 0
        self.player2_name = player2_name or ("🤖 FG-OS AI" if is_ai else "Waiting...")
        self.is_ai = is_ai
        
        # Boards
        self.board1 = BattleshipBoard()  # Player 1's board
        self.board2 = BattleshipBoard()  # Player 2's board
        
        # Game state
        self.current_turn = 1  # 1 or 2
        self.game_state = "placing"  # placing, playing, finished
        self.winner_id = None
        
        # AI targeting
        self.ai_target_list = []
        self.ai_in_hunt = False
        self.ai_last_hit = None
        self.ai_hunt_direction = None  # 'h' or 'v' for horizontal/vertical hunt
    
    def setup_ships(self, board: BattleshipBoard):
        """Place ships randomly on board"""
        for ship_name, size in self.SHIPS:
            ship = Ship(ship_name, size)
            placed = False
            attempts = 0
            
            while not placed and attempts < 100:
                horizontal = random.choice([True, False])
                row = random.randint(0, 9)
                col = random.randint(0, 9)
                
                # Validate placement
                if not (0 <= (row if not horizontal else row) < 10 and 
                        0 <= (col if horizontal else col + size) <= 10):
                    attempts += 1
                    continue
                
                ship.place(row, col, horizontal)
                
                # Check for overlaps
                occupied = set()
                for s in board.ships:
                    occupied.update(s.positions)
                
                if not any(pos in occupied for pos in ship.positions):
                    board.add_ship(ship)
                    placed = True
                
                attempts += 1
    
    def shoot(self, shooter_is_player1: bool, row: int, col: int) -> Tuple[str, Optional[Ship], bool, bool]:
        """
        Execute a shot.
        Returns: (result, ship, is_sunk, all_sunk)
        """
        target_board = self.board2 if shooter_is_player1 else self.board1
        result, ship = target_board.shoot(row, col)
        
        if result == 'hit' and ship:
            is_sunk = ship.is_sunk()
            all_sunk = target_board.all_sunk()
            return (result, ship, is_sunk, all_sunk)
        
        return (result, ship, False, False)
    
    def ai_get_target(self) -> Tuple[int, int]:
        """Smart AI targeting"""
        # If hunting, try to extend the hit pattern
        if self.ai_in_hunt and self.ai_last_hit:
            row, col = self.ai_last_hit
            
            # Try adjacent cells in priority order
            candidates = [(row, col + 1), (row + 1, col), (row, col - 1), (row - 1, col)]
            random.shuffle(candidates)
            
            for nr, nc in candidates:
                if 0 <= nr < 10 and 0 <= nc < 10:
                    if (nr, nc) not in self.board1.hits and (nr, nc) not in self.board1.misses:
                        return (nr, nc)
        
        # Smart targeting: prefer areas with fewer nearby misses
        best_score = -999
        best_pos = (random.randint(0, 9), random.randint(0, 9))
        
        for _ in range(40):
            row = random.randint(0, 9)
            col = random.randint(0, 9)
            
            if (row, col) in self.board1.hits or (row, col) in self.board1.misses:
                continue
            
            # Score: penalize areas near misses
            nearby_misses = sum(1 for dr in [-1, 0, 1] for dc in [-1, 0, 1]
                               if 0 <= row + dr < 10 and 0 <= col + dc < 10 
                               and (row + dr, col + dc) in self.board1.misses)
            
            score = -nearby_misses
            if score > best_score:
                best_score = score
                best_pos = (row, col)
        
        return best_pos
    
    def get_board_string(self, for_player1: bool, show_opponent_ships: bool = False) -> str:
        """Generate beautiful ASCII board with enhanced styling"""
        my_board = self.board1 if for_player1 else self.board2
        opponent_board = self.board2 if for_player1 else self.board1
        
        def format_board(board, reveal_ships=False, title=""):
            lines = []
            lines.append("")
            lines.append(f"  {title}")
            lines.append("     A   B   C   D   E   F   G   H   I   J")
            lines.append("   " + "+" + ("---+" * 10))
            
            for row in range(10):
                row_num = row + 1
                cells = []
                for col in range(10):
                    cell = board.get_cell(row, col, reveal_ships=reveal_ships)
                    cells.append(f" {cell} ")
                lines.append(f" {row_num:2d} |" + "|".join(cells) + "|")
                lines.append("   " + "+" + ("---+" * 10))
            
            return "\n".join(lines)
        
        player_name = "YOUR FLEET" if for_player1 else "OPPONENT'S FLEET"
        opp_name = "OPPONENT'S FLEET" if for_player1 else "YOUR FLEET"
        
        board1_str = format_board(my_board, reveal_ships=True, title=f"[1] {player_name}")
        board2_str = format_board(opponent_board, reveal_ships=show_opponent_ships, title=f"[2] {opp_name}")
        
        return board1_str + "\n" + board2_str
    
    def get_ship_status_embed(self, for_player1: bool) -> discord.Embed:
        """Create beautiful ship status embed"""
        board = self.board1 if for_player1 else self.board2
        
        embed = discord.Embed(
            title="🚢 Fleet Status",
            color=0x1E90FF if for_player1 else 0xFF6B6B
        )
        
        for ship in board.ships:
            status = "✅ SUNK" if ship.is_sunk() else "🟢 Active"
            embed.add_field(
                name=f"{ship.name} ({ship.size})",
                value=f"{ship.get_health()} {status}",
                inline=False
            )
        
        sunk = board.count_sunk()
        embed.set_footer(text=f"Ships remaining: {len(board.ships) - sunk}/{len(board.ships)}")
        
        return embed
    
    def get_game_status_embed(self) -> discord.Embed:
        """Create game status embed"""
        embed = discord.Embed(
            title="🎮 Battleship Status",
            color=0xFFD700
        )
        
        if self.game_state == "finished":
            winner_name = self.player1_name if self.winner_id == self.player1_id else self.player2_name
            embed.add_field(
                name="🏆 GAME OVER",
                value=f"**{winner_name}** WINS!",
                inline=False
            )
            embed.color = 0x00FF00
        else:
            current_name = self.player1_name if self.current_turn == 1 else self.player2_name
            embed.add_field(
                name="📍 Current Turn",
                value=f"**{current_name}**",
                inline=False
            )
        
        p1_sunk = self.board1.count_sunk()
        p2_sunk = self.board2.count_sunk()
        
        embed.add_field(
            name=f"{self.player1_name}",
            value=f"Ships sunk: {p1_sunk}/5",
            inline=True
        )
        embed.add_field(
            name=f"{self.player2_name}",
            value=f"Ships sunk: {p2_sunk}/5",
            inline=True
        )
        
        embed.add_field(
            name="📋 How to Play",
            value="`/fire A 5` — Attack column A, row 5\n`/gameboard` — View boards\n`/quitgame` — Forfeit",
            inline=False
        )
        
        return embed


# Active games storage
active_games = {}
game_id_counter = 0


def create_game(player1_id: int, player1_name: str, 
                player2_id: Optional[int] = None, player2_name: Optional[str] = None,
                is_ai: bool = False, channel: discord.TextChannel = None) -> int:
    """Create a new game and return game_id"""
    global game_id_counter
    game_id_counter += 1
    
    game = BattleshipGame(player1_id, player1_name, player2_id, player2_name, is_ai, channel)
    game.game_id = game_id_counter
    
    game.setup_ships(game.board1)
    game.setup_ships(game.board2)
    game.game_state = "playing"
    game.current_turn = 1
    
    active_games[game_id_counter] = game
    return game_id_counter


def get_game(player_id: int) -> Optional[BattleshipGame]:
    """Find active game for a player"""
    for game in active_games.values():
        if game.player1_id == player_id or game.player2_id == player_id:
            return game
    return None


def get_game_by_id(game_id: int) -> Optional[BattleshipGame]:
    """Get game by ID"""
    return active_games.get(game_id)


def end_game(game_id: int):
    """Remove game from active list"""
    active_games.pop(game_id, None)
