"""Text for /help. Pure functions, so they can be tested.

`networks` is a list of (name, native coin symbol) for the networks that are switched on.
"""
import html

TOPICS = [
    ("start", "🚀 Getting started"),
    ("home", "🏠 Home & balances"),
    ("view", "🖼 Home view"),
    ("deposit", "📥 Deposit"),
    ("send", "💸 Sending"),
    ("history", "📜 History"),
    ("tip", "🎁 Tipping in groups"),
    ("rain", "🌧 Rain"),
    ("quiz", "🧠 Quiz"),
    ("earn", "💎 Earn"),
    ("airdrop", "🪂 Airdrops"),
    ("share", "🔗 Share a code"),
    ("trade", "📈 Trade"),
    ("vote", "🗳 Vote & GAS"),
    ("bridge", "🌉 Bridge"),
    ("tokens", "➕ Adding tokens"),
    ("keys", "👛 Wallets"),
    ("notify", "🔔 Notifications"),
    ("safety", "🔐 Keys & safety"),
    ("assistant", "🤖 AI Assistant"),
    ("support", "🆘 Support"),
]


def topic_keys(bridge_on: bool) -> list[tuple[str, str]]:
    return [t for t in TOPICS if bridge_on or t[0] != "bridge"]


def main_text(networks: list[tuple[str, str]], bridge_on: bool) -> str:
    names = " · ".join(html.escape(n) for n, _ in networks)
    lines = [
        "❓ <b>NeoRedPill Wallet · Help</b>",
        "",
        "Your own crypto wallet inside Telegram: hold, send, tip and bridge without leaving the chat.",
        "",
        "<b>Networks</b>",
        names,
        "",
        "<b>What this bot can do</b> (tap a button below for the full guide)",
        "🏠 <b>Home</b>: your balances with prices and USD value, as text or as a picture card (/view)",
        "📥 <b>Deposit</b>: your personal address on every network",
        "📈 <b>Trade</b>: where a token is listed, and trading it on a DEX (a quote with what you receive and the fees)",
        "💸 <b>Send</b>: to an @username or any wallet address",
        "📜 <b>History</b>: every deposit, send, tip and bridge, plus what is still pending",
        "🎁 <b>Tip</b>: in a group, to several people at once",
        "🌧 <b>Rain</b> and 🧠 <b>Quiz</b>: token giveaways and prize quizzes for your group",
        "💎 <b>Earn</b>: claim GAS for holding NEO, vote on Neo, and join 🪂 airdrops (tasks and token rewards)",
    ]
    if bridge_on:
        lines.append("🌉 <b>Bridge</b>: move GAS between Neo N3 and Neo X")
    lines += [
        "🪙 <b>Add / Remove token</b>: add any token by its contract address, or remove one",
        "👛 <b>Wallets</b>: import, export, switch or delete your wallets",
        "🔔 <b>Notifications</b>: a message for every deposit, withdrawal, transfer and tip",
        "",
        "<b>Commands</b>",
        "/start · /home · /wallets · /send · /history · /pending · "
        + ("/bridge · " if bridge_on else "")
        + "/trade · /view · /airdrop · /addtoken · /tip · /rain · /quiz · /help · /cancel",
        "",
        "Tap a feature below for a full explanation: what it does and how to use it 👇",
        "Questions? Tap <b>🤖 AI Assistant</b> and just ask. Need a person? Tap <b>🆘 Support</b> (@aziz1683 or @btc_mahadi).",
    ]
    return "\n".join(lines)


def topic_text(key: str, networks: list[tuple[str, str]], bridge_on: bool) -> str | None:
    if key == "start":
        fees = "\n".join(f"• {html.escape(sym)} on {html.escape(name)}" for name, sym in networks)
        return (
            "🚀 <b>Getting started</b>\n\n"
            "1. Send /start. Your wallets are created for you.\n"
            "2. Tap <b>📥 Deposit</b> to see your addresses: one per kind of network (Neo N3, EVM networks share one, Solana has its own).\n"
            "3. From an exchange or another wallet, send tokens to the address of the <b>matching network</b>. "
            "I message you when they arrive.\n"
            "4. Tap <b>🏠 Home</b> any time to see your balances.\n"
            "5. The first time, choose how Home looks: <b>📝 Text</b> or <b>🖼 Photo card</b>. Change it any time with /view.\n"
            "6. Your shortcuts are in the keyboard under the chat box. Every feature has a guide in this Help.\n\n"
            "Every transfer costs a small fee, paid in the network's own coin, so keep a little of it:\n"
            f"{fees}\n\n"
            "💡 Try a small amount first on any new network."
        )
    if key == "send":
        return (
            "💸 <b>Sending</b>\n\n"
            "1. Tap <b>💸 Send</b>, pick the <b>network</b>, then the <b>token</b>. (With only one network, you go straight to the tokens.)\n"
            "2. Enter an <b>@username</b> (they must have started this bot) or a wallet address on the <b>same network</b>.\n"
            "3. Enter the amount, or <b>max</b>.\n"
            "4. Check the summary, then confirm.\n\n"
            "The summary also shows the <b>estimated network fee</b> in the network's own coin.\n\n"
            "Transfers are real and can't be undone. Check the address carefully. "
            "The fee comes out of your wallet in the network's coin, and a little is always kept back for it."
        )
    if key == "history":
        return (
            "📜 <b>History</b>\n\n"
            "Tap <b>📜 History</b> (or send /history) to see everything that happened in your wallet, newest first: "
            "deposits, transfers you received, sends, tips and bridges. Use ◀ Newer / Older ▶ to move between pages.\n\n"
            "⏳ <b>Pending</b> (or /pending) lists transactions that are still waiting for the network, including a bridge "
            "that is on its way. I check them every minute and message you when they settle. "
            "<b>Check &amp; refresh</b> asks the network right now. There is nothing to approve or cancel: a transaction already sent to the network can't be cancelled, it only waits to be confirmed. "
            "<b>🧹 Clear stuck ones</b> removes records that were never sent (they show \"No transaction was recorded\"); they are marked failed and your balances are not affected.\n\n"
            "• <b>Confirmed</b>: it went through.\n"
            "• <b>Pending / not confirmed yet</b>: wait, and do not send the same thing again.\n"
            "• <b>Failed</b>: it did not go through; a small fee may still have been used.\n"
            "Each line has a <i>view tx</i> link to the block explorer."
        )
    if key == "tip":
        return (
            "🎁 <b>Tipping in groups</b>\n\n"
            "Add me to a group, then write:\n"
            "<code>/tip @alice 5 GAS</code>: 5 GAS to Alice\n"
            "<code>/tip @alice @bob 5 USDT</code>: 5 USDT to <b>each</b>\n"
            "<code>/tip @alice @bob 10 USDT split</code>: 10 USDT shared equally\n"
            "Reply to someone's message with <code>/tip 5 GAS</code> to tip them.\n"
            "If the symbol exists on several networks, add the network: <code>/tip @alice 5 USDT bsc</code>.\n\n"
            "I show a summary with <b>Yes, send</b> / <b>Cancel</b>. Only you can press it, and it expires after 5 minutes.\n\n"
            "• Up to 10 people at once.\n"
            "• Everyone you tip must have started this bot once.\n"
            "• Each tip is its own real transfer and you pay each fee. The summary shows the <b>estimated network fee</b> in that network's own coin (GAS, BNB, ETH, SOL ...) with its price in dollars, for all the people together.\n"
            "• Nobody's address or balance is shown in the group."
        )
    if key == "events":
        return (
            "🌧 <b>Rain and 🧠 quiz</b>\n\n"
            "<b>How to start one</b>\n"
            "1. Tap <b>Prepare a rain</b> or <b>Prepare a quiz</b> below. I show your tokens and ask you step by step "
            "(token, amount, how many people, equal or random; for a quiz the prize and each question). Just answer.\n"
            "2. Each one gets its own <b>code</b>, like <code>R-7K2Q</code> (rain) or <code>Q-3HDP</code> (quiz). "
            "You can prepare as many as you like.\n"
            "3. In any group where I am a member, send <code>/rain R-7K2Q</code> (or <code>/quiz Q-3HDP</code>). "
            "You do <b>not</b> need to be an admin, and I do not ask again: it starts at once, and the message has a "
            "<b>Cancel</b> button for you. With only one waiting you can send just <code>/rain</code>.\n"
            "<b>Share a code:</b> you can give a code to a friend and he can send it in his group. Whoever posts a code starts it once, and the prizes come from <b>your</b> wallet, so share a code only with people you trust. Both of you can press its Cancel button.\n"
            "<b>📋 My rains & quizzes</b> shows every code with its state: waiting, live (and where), or finished. "
            "You can cancel a waiting one, or stop a live rain.\n"
            "Group admins can also skip the preparing and type it in the group, like <code>/rain 100 NRP 10 random</code>; "
            "I then ask them to confirm in private.\n\n"
            "<b>Rain</b>: a first-come, first-served airdrop with a Claim button.\n"
            "<code>100 NRP</code>: shared equally by the first 5 people · <code>100 NRP 10</code>: first 10 people · "
            "<code>100 NRP 10 random</code>: a different random share for everyone. Up to 20 people.\n"
            "It ends when everyone has claimed, or when you press Cancel. Each person is paid the moment they claim, "
            "and a last message tells you how fast it went.\n\n"
            "<b>Quiz</b>: multiple choice, up to 10 questions. When you prepare one you choose:\n"
            "• <b>Who wins</b>: ⚡ the fastest correct answers on every question (1 to 10 winners), or "
            "🏆 the best scores at the end (the top 1 to 50). Players with the same score are ranked by who "
            "answered faster overall; I keep those times secret.\n"
            "• <b>The reward</b>: 💰 fixed (every winner gets the same amount), 🎲 random (a total amount is split "
            "randomly among the winners) or 🥇 by place (like <code>1000, 500, 250</code>: 1000 for 1st, 500 for 2nd, "
            "250 for 3rd; the list sets how many winners there are).\n"
            "• <b>The timer</b>: how long each question stays open, from 5 seconds to 10 minutes (10 s, 20 s, 30 s, "
            "1 min... or your own).\n"
            "• The questions and their answers (a wrong tap locks that player out of the question).\n\n"
            "<b>Who can play</b>: only people who have already started me (opened me in private and pressed Start). "
            "That way every winner has a wallet and the prize is sent to them automatically when the quiz ends.\n\n"
            "• One rain and one quiz at a time per group.\n"
            "• The tokens stay in your wallet; prizes are sent as normal transfers and you pay the fees. "
            "Keep the balance until the event ends, or the payouts stop.  Quiz prizes are paid when the quiz ends.\n"
            "• Winners get a wallet automatically; they press Start in private to see it.\n"
            "• If the bot restarts, the codes you prepared are forgotten and a running event stops."
        )
    if key == "earn":
        return (
            "💎 <b>Earn</b>\n\n"
            "Tap <b>💎 Earn</b> (or send /earn). It has three buttons:\n"
            "• <b>💰 Claim your GAS</b>: collect the GAS you earn by holding NEO and by voting, one by one or all at once.\n"
            "• <b>🗳 Vote</b>: vote for a Neo candidate to earn more GAS. See the Vote help.\n"
            "• <b>🪂 Airdrops</b>: finish tasks and win token rewards. See the Airdrops help."
        )
    if key == "vote":
        return (
            "🗳 <b>Vote &amp; earn GAS</b>\n\n"
            "Neo N3 holders vote for the candidates who run the network. If your candidate is elected, you earn GAS. "
            "You choose how much NEO to vote with, and your NEO stays yours.\n\n"
            "1. Tap <b>💎 Earn</b> → <b>🗳 Vote</b> (or send /vote). The first page explains how it works and how you earn.\n"
            "2. Tap <b>Vote for a candidate</b>. You see the 21 council candidates, one button each with its rank, name and votes. You can look without having any NEO.\n"
            "3. Tap a candidate to see its page: status, votes cast, share of votes, public key, address and a rough GAS estimate.\n"
            "4. Tap <b>Vote for this candidate</b>, then tap an amount or <b>type how much NEO</b> you want to vote with, and confirm.\n"
            "5. You can vote for several candidates with different amounts. <b>📋 My votes</b> shows each one, lets you take the NEO back, or finish a vote that stopped halfway.\n"
            "6. To collect your GAS, tap <b>💎 Earn</b> → <b>💰 Claim your GAS</b>. It shows the GAS from holding NEO and from each vote, with a claim button for each, and one to claim everything at once.\n\n"
            "• You need NEO in your Neo N3 wallet, and a little GAS (about 0.15) for the network fees.\n"
            "• A Neo account can only vote for one candidate. So for each candidate I keep a small <b>vote wallet</b> for you, and move the NEO you choose into it. It is yours, and you can take the NEO back any time.\n"
            "• Each step is a real transaction with a small fee (about 0.02 GAS), and I wait for each one before the next.\n"
            "• You earn only while a candidate is in the top 21. If it drops out, vote for another.\n"
            "• Holding NEO earns GAS by itself, even if you never vote.\n"
            "• The GAS you earned from holding NEO is paid out automatically whenever that NEO moves. So when you vote, the GAS your NEO had earned lands in your main wallet. It is your own earned reward, not a gift or an extra charge. It happens inside the same transaction, so there is no extra claiming fee. Only a manual <b>Claim your GAS</b> is its own transaction with its own small fee.\n"
            "• Voting does not lock your NEO. Taking it back from <b>📋 My votes</b> also pays out the GAS that vote earned.\n"
            "• The reward changes with how many people vote, so I can't promise an amount."
        )
    if key == "bridge" and bridge_on:
        return (
            "🌉 <b>Bridge</b>\n\n"
            "Moves a coin or token from one of your wallets to another network.\n\n"
            "1. Tap <b>🌉 Bridge</b> and pick the network to move <b>from</b>.\n"
            "2. Pick the network to move <b>to</b> (Solana is included).\n"
            "3. Pick the coin or token and the amount (tap a share or type it).\n"
            "4. I list every bridge that can do it, with what you receive, the fee and the time. ⭐ is the best price, ⚡ the fastest.\n"
            "5. Pick one, check the details and confirm.\n\n"
            "• Bridges go through LI.FI, which compares many bridges. Only networks it supports appear.\n"
            "• Between EVM networks and Solana (both directions), Relay is also compared when it is switched on, and the best price is listed first.\n"
            "• Neo N3 ↔ Neo X uses the official Neo bridge and moves GAS only.\n"
            "• Bridging <i>from</i> Solana is not available yet.\n"
            "• Keep a little of the sending network's coin for fees. Try a small amount first.\n"
            "• I message you when it arrives. It can take a few minutes."
        )
    if key == "tokens":
        return (
            "➕ <b>Adding tokens</b>\n\n"
            "NEO, GAS, NRP, FRANK and each network's own coin are already there. For anything else:\n"
            "1. Tap <b>🪙 Add / Remove token</b>, then <b>➕ Add a token</b>, and pick the network.\n"
            "2. Paste the token's <b>contract address</b>.\n"
            "3. Check the name and symbol I read from the contract, then confirm.\n\n"
            "⚠️ Anyone can create a token with a famous name. Only add contract addresses from official sources.\n"
            "Tokens you haven't added stay hidden, so spam tokens don't clutter your wallet. "
            "Whenever any token arrives, on any network, I message you; if it isn't on your list I ask <b>Do you want to add this token?</b> with a Yes button, so you never need to copy a contract address. "
            "Added tokens appear on Home and can be sent like any other.\n\n"
            "🛡 <b>Scam filter:</b> on the EVM networks I check a token before adding it, by simulating a buy and a sell. A <b>honeypot</b> "
            "(you can buy it but not sell it) or a token with a huge trade tax is refused, and a token that \"sends itself\" to people is "
            "treated as a spam airdrop with no Yes button. If the check service is down I say so and leave the choice to you. "
            "No filter is perfect, so only add tokens from official sources.\n\n"
            "🗑 <b>Remove a token</b> (under 🪙 Add / Remove token, or /removetoken) takes a token you added off your list, "
            "for example a scam token or one you don't use. Your balance is untouched and you can add it again."
        )
    if key == "safety":
        return (
            "🔐 <b>Keys &amp; safety</b>\n\n"
            "• Your keys are stored encrypted by this bot. Wallet details only ever appear in a private chat.\n"
            "• <b>Export</b> shows a private key for 2 minutes, then deletes it. Tap 📋 Copy key to copy it (screenshots and forwarding are blocked). Anyone who has the key controls the funds.\n"
            "• <b>Never</b> share your key. Nobody legitimate will ask for it, including admins.\n"
            "• <b>Import</b> adds a wallet and keeps your old one; <b>Switch wallet</b> goes between them. <b>Delete</b> removes a wallet for good after two confirmations, so export it first.\n"
            "• Keep your own backup of your keys.\n"
            "• Always send to the address of the right network. A token sent to the wrong network can be lost."
        )
    if key == "home":
        return (
            "🏠 <b>Home &amp; balances</b>\n\n"
            "<b>What it does:</b> shows everything you hold, network by network, with the price and the value in USD.\n\n"
            "<b>How to use it:</b>\n"
            "1. Tap <b>🏠 Home</b> (or send /home).\n"
            "2. Read your balances. A network that is slow to answer shows as unavailable; tap Home again in a moment.\n"
            "3. From Home you can jump to Deposit, Send, History and the rest.\n\n"
            "• Only tokens on your list are shown. Add more with <b>🪙 Add / Remove token</b>.\n"
            "• Prices come from Bifinance, CoinGecko, Binance and (for tokens you added) the biggest real DEX pool such as "
            "PancakeSwap. A token that can't be matched by its exact contract address gets no price, so fakes never show a made-up value.\n"
            "• If a price is missing for a moment, the last known price is used for a few hours.\n"
            "• Prices are for information only and can differ a little from an exchange.\n\n"
            "Want it as a picture? See <b>🖼 Home view</b> in this Help."
        )
    if key == "view":
        return (
            "🖼 <b>Home view</b>\n\n"
            "<b>What it does:</b> lets you choose how your Home page looks.\n\n"
            "• <b>📝 Text</b>: the classic page, fast and simple, with the buttons under the message.\n"
            "• <b>🖼 Photo card</b>: a picture with your total, a chart of your networks and every token with its balance, "
            "price and USD value. The message is only the picture, and all actions are in the keyboard under the chat box.\n\n"
            "<b>How to use it:</b>\n"
            "1. Send /view (or tap <b>🖼 Home view</b>).\n"
            "2. Tap <b>📝 Text</b> or <b>🖼 Photo card</b>. Your choice is saved.\n"
            "3. With more than 8 tokens the photo has pages: use <b>◀ ▶</b> under it.\n"
            "4. Tap the photo to open it full screen.\n\n"
            "• Each token shows its real logo when I can find one (matched by its contract address). A token with no logo "
            "gets a colored letter. Logos are saved after the first time, so they appear instantly later.\n"
            "• The photo card is drawn on the server, so it can take a moment longer than text.\n"
            "• If the picture can't be made, I show the text page instead."
        )
    if key == "trade":
        return (
            "📈 <b>Trade</b>\n\n"
            "<b>What it does:</b> shows where you can buy or sell a token you hold, and gives you its contract address, "
            "so you don't have to look for it.\n\n"
            "<b>How to use it:</b>\n"
            "1. Tap <b>📈 Trade</b> (or send /trade).\n"
            "2. Pick the network, then the token.\n"
            "3. Read the contract address (tap it to copy), the exchanges (CEX) and DEXs that list it, and the steps to trade.\n"
            "4. Use the link buttons to open a DEX or a chart.\n\n"
            "<b>Stablecoins:</b> every EVM network has USDT and USDC (and DAI on Ethereum), Solana has USDC and USDT, and Neo N3 has Flamingo's USDT. Open the network's own coin, for example BNB, and tap <b>💵 Trade BNB for USDT</b> to buy or sell it against that stablecoin. On Solana you can hold and send them; trading there is not built yet.\n\n"
            "<b>Trading inside the bot:</b> under the token you see a <b>💱 Trade here with …</b> button for every supported exchange that has a pool for it, once per exchange: when an exchange has several pool versions, I show only the one that pays the most, and the best exchange comes first "
            "(PancakeSwap V2 and V3, Uniswap V3, SushiSwap, BiSwap and ApeSwap on BNB Chain, Uniswap V2, V3 and V4 and SushiSwap on Ethereum, Uniswap V3, QuickSwap and SushiSwap on Polygon, Uniswap V3 and SushiSwap on Arbitrum; on Base, Optimism and Avalanche: Uniswap V2 and V3 (and V4 on Base and Optimism), plus SushiSwap, BaseSwap and PancakeSwap V3 on Base; V4 only for pools without hooks, and selling there needs two small approvals). Tap it, "
            "choose Buy (with the network's coin) or Sell (for the coin). I show your balance, so you can tap <b>25% / 50% / 75% / Max</b> or type the amount. I show what you <b>receive</b>, the "
            "<b>minimum</b> after slippage, the <b>trade fee</b>, the <b>price impact</b> and the estimated network fee, with dollar values. "
            "Pick 0.5%, 1% or 3% slippage (1% is the default), then tap <b>Confirm</b>. The swap runs from your own wallet and shows "
            "the transaction link; it also appears in your History as <b>🔄 Swap</b>.\n"
            "Safety: the DEX router is checked on-chain first; the network dry-runs the swap and I refuse it if it would fail; if the price "
            "moved since the quote I ask you again. Selling needs a one-off approval for <b>exactly</b> the amount you sell, never unlimited. "
            "Centralized exchanges (like Bifinance) are only shown for information: I can't trade on them.\n"
            "On <b>Neo N3</b> every token (except GAS itself) has <b>💱 Trade here with FTW Swap</b>: you buy it with GAS or sell it for GAS. "
            "NEO can only be traded in whole units. You receive an exact amount and pay at most the price plus your slippage; the real Network fee is shown after the trade. "
            "<b>💱 Trade here with Flamingo</b> works for any token with a Flamingo pool (GAS to the token and back): the first time I search Flamingo for the token's route (a few seconds, nothing is sent) and remember it. You pay an exact amount and I set the least you will accept. "
            "Neo X and Solana trading inside the bot are coming later; for now they show link buttons.\n\n"
            "• <b>DEX</b> pools come from DexScreener (PancakeSwap, Uniswap and others), matched by the exact contract address, "
            "so fakes with the same name are not shown. For BNB Chain tokens there is also a Poocoin chart link.\n"
            "• <b>Exchanges (CEX)</b> come from CoinGecko, and Bifinance for the tokens it lists.\n"
            "• For <b>Neo N3</b> tokens I show FTW Swap and Flamingo. If a token has no pool on one of them I tell you when you press the button.\n"
            "• A network's own coin (BNB, ETH, GAS...) has no contract address, so I list where it trades instead.\n"
            "• If a source doesn't answer, I say so and the list may be incomplete.\n"
            "• Test with a small amount first, and compare the quote with the DEX's own screen."
        )
    if key == "deposit":
        names = " · ".join(html.escape(n) for n, _ in networks)
        return (
            "📥 <b>Deposit</b>\n\n"
            "<b>What it does:</b> shows your personal wallet address on every network, so you can receive tokens.\n\n"
            "<b>How to use it:</b>\n"
            "1. Tap <b>📥 Deposit</b> (or send /wallets).\n"
            "2. Copy the address of the network you want. Neo N3 has its own address, all EVM networks share one, and Solana has its own.\n"
            "3. In your exchange or other wallet, send to that address <b>on the same network</b>.\n"
            "4. I message you when it arrives.\n\n"
            f"Networks available: {names}.\n"
            "⚠️ Sending to the wrong network can lose the funds. Try a small amount first."
        )
    if key == "rain":
        return (
            "🌧 <b>Rain</b>\n\n"
            "<b>What it does:</b> a first-come, first-served airdrop in a group. People tap a <b>Claim</b> button and are paid at once from your wallet.\n\n"
            "<b>How to use it:</b>\n"
            "1. Tap <b>🌧 Rain &amp; 🧠 Quiz</b> on Home, then <b>Prepare a rain</b>.\n"
            "2. Answer my questions: the network and token (network first, then token), the amount, how many people (up to 20), and equal or random shares.\n"
            "3. You get a code like <code>R-7K2Q</code>.\n"
            "4. In any group where I am a member, send <code>/rain R-7K2Q</code>. You do not need to be an admin and it starts at once.\n\n"
            "• <b>Equal</b>: everyone gets the same. <b>Random</b>: a different share for everyone, adding up to your total.\n"
            "• It has no time limit. It ends when everyone has claimed or when you press <b>Cancel</b>. A last message tells you how fast it went.\n"
            "• If a group admin posts it, the message is pinned while it runs and unpinned at the end. A rain posted by a normal member is not pinned.\n"
            "• One rain at a time per group. You cannot claim your own rain.\n"
            "• Group admins can also type it directly, like <code>/rain 100 NRP 10 random</code>; I then ask them to confirm in private.\n"
            "• Your tokens stay in your wallet until someone claims, and you pay the network fees. Keep the balance until the rain ends.\n"
            "• The summary shows the <b>estimated network fee</b> in the network's own coin (BNB on BNB Chain, GAS on Neo, SOL on Solana ...), with its price in dollars, worked out for all the people. For example a rain for 10 people costs 10 transfers, so ten times one transfer's fee. This is the most it can cost, if everyone claims."
        )
    if key == "quiz":
        return (
            "🧠 <b>Quiz</b>\n\n"
            "<b>What it does:</b> a multiple-choice quiz in a group, with real token prizes for the winners.\n\n"
            "<b>How to use it:</b>\n"
            "1. Tap <b>🌧 Rain &amp; 🧠 Quiz</b> on Home, then <b>Prepare a quiz</b>.\n"
            "2. Pick <b>who wins</b>: ⚡ the fastest correct answers on every question (1 to 10 winners), or 🏆 the best scores at the end (the top 1 to 50). With equal scores, the faster player ranks higher; I keep those times secret.\n"
            "3. Pick <b>the reward</b>: 💰 fixed (same for every winner), 🎲 random (a total split randomly) or 🥇 by place (like <code>1000, 500, 250</code> for 1st, 2nd and 3rd).\n"
            "4. Pick <b>the timer</b> per question, from 5 seconds to 10 minutes.\n"
            "5. Send each question with its right answer and 1 to 3 wrong ones (up to 10 questions).\n"
            "6. You get a code like <code>Q-3HDP</code>. In any group where I am a member, send <code>/quiz Q-3HDP</code>.\n\n"
            "• <b>Only people who have already started me</b> (opened me in private and pressed Start) can play, so every winner has a wallet.\n"
            "• A wrong tap locks that player out of the question.\n"
            "• Prizes are sent automatically from your wallet when the quiz ends, and you pay the fees. You cannot play your own quiz.\n"
            "• One quiz at a time per group. You can cancel with the button on the first message."
        )
    if key == "airdrop":
        return (
            "🪂 <b>Airdrops</b>\n\n"
            "A timed event where people finish tasks and earn token rewards. Open it with <b>💎 Earn</b> → <b>🪂 Airdrops</b> (or /airdrop).\n\n"
            "<b>Taking part</b>\n"
            "1. Tap <b>Active airdrops</b> (or <b>Upcoming</b>): each shows who runs it, the total reward, how you can win and the time left. Or open the link a creator shared.\n"
            "2. Tap a task, tap <b>Open</b>, do it, then come back:\n"
            "• <b>Telegram channel or group</b>: tap <b>Verify</b>. I check it.\n"
            "• <b>X, Discord, Instagram, YouTube, Reddit, TikTok, Facebook, GitHub</b>: tap <b>Send my username</b> and type yours. It is accepted at once, or the creator approves it.\n"
            "• <b>Question</b>: tap <b>Answer</b>. I check it.\n"
            "• <b>Invite friends</b>: tap <b>My invite link</b>, share it, then <b>Check my invites</b>. A friend counts once they finish a task.\n"
            "• <b>Daily check-in</b>: tap <b>Check in today</b> once a day, on enough different days.\n"
            "• <b>Token balance</b>: keep the required amount in your wallet here and tap <b>Verify balance</b>.\n"
            "• <b>Website</b>: confirm that you visited.\n"
            "3. <b>✅ Verify all</b> checks every task I can check, in one tap.\n"
            "4. Finish every task to qualify. Rewards go straight to your wallet here.\n\n"
            "<b>Finding airdrops</b>\n"
            "• <b>Active airdrops</b> and <b>Upcoming</b> are cards showing the banner, who runs it, the reward (amount, first come first served, lucky draw or leaderboard), the tasks and the time left or start time. A <b>Funded</b> badge means the creator's wallet held the reward when it was checked.\n"
            "• <b>My airdrops</b> lists the ones you made or joined, with your progress.\n"
            "• New airdrops are not announced to everyone by default: our team (Aziz and Mahadi) gets a message with <b>📣 Notify everyone</b> or <b>⏭ Skip</b>. Either way the airdrop stays in the list and anyone can join.\n\n"
            "<b>How rewards work</b> (a creator can use one or more)\n"
            "• ⚡ <b>First come, first served</b>: the first people to finish every task each get a fixed amount. No points.\n"
            "• 🎲 <b>Lucky draw</b>: when it ends, random winners among everyone who finished every task. No points.\n"            "• In a lucky-draw or first-come airdrop, <b>👥 Participants & winners</b> lists everyone who joined (in the order they joined, no ranks). When it ends, the <b>winner list</b> is sent to everyone who took part and to the creator, and only the winners get the reward. Ranks are used only in leaderboard (points) airdrops.\n"
            "• 🏆 <b>Leaderboard</b>: tasks give points, and the top scorers win prizes. Points only exist when there is a leaderboard.\n\n"
            "<b>Making one</b>\n"
            "1. <b>Create an airdrop</b>: title, description, optional 🖼 banner, <b>time frame</b> and token (pick the network first, then the token). For the time frame tap a length (or type <code>15m</code>, <code>1h</code>, <code>2d</code>), or <b>Pick exact start & end time</b> and type dates in your time zone.\n"
            "2. Choose the reward type(s), amounts, and ⚡ automatic or ✋ manual payment. Choose <b>Funded</b> (I check your wallet and show a badge) or <b>Pay when due</b>.\n"
            "3. <b>Add tasks</b> (up to 12): platform, action, link. For a Telegram chat, <b>make me an admin there first</b>.\n"
            "4. <b>🔗 Share link</b> works even before publishing (people see 'coming soon'). Post it on X or anywhere, then add <b>your post's link</b> as a Like, Repost or Quote task.\n"
            "5. <b>Publish</b>. It goes live at the start time you chose. It is always in the Airdrops list. Our team then decides whether to announce it to <b>every user</b>; if not, it still stays in the list.\n\n"
            "<b>As the creator you can</b>\n"
            "• 👥 <b>People</b>: everyone who joined, what they sent, their progress. You also get a message when someone joins. 📤 <b>Export CSV</b> saves the full list with wallet addresses.\n"
            "• ✏️ <b>Edit title / description</b>, 🖼 <b>Banner</b> and ➕ tasks can be changed while it runs.\n"
            "• 🔍 <b>Review</b>: approve or reject usernames. • 💸 <b>To pay</b>: pay rewards or mark them paid.\n"
            "• 🏁 <b>End now</b>, 🚫 <b>Cancel</b>, 🗓 <b>Time frame</b> (drafts).\n\n"
            "<b>Good to know</b>\n"
            "• Rewards come from the creator's wallet. <b>Funded</b> means the balance was checked, not locked away.\n"
            "• I can check by myself: Telegram joins, questions, invites, daily check-ins and token balances. For other sites a username is accepted at once or approved by the creator.\n"
            "• You can't join your own airdrop. One username counts for one person per task."
        )
    if key == "share":
        return (
            "🔗 <b>Share a code</b>\n\n"
            "<b>What it does:</b> lets a friend start your prepared rain or quiz in his own group.\n\n"
            "<b>How to use it:</b>\n"
            "1. Prepare a rain or quiz and get its code (<code>R-…</code> or <code>Q-…</code>).\n"
            "2. Send the code to your friend.\n"
            "3. He posts <code>/rain CODE</code> or <code>/quiz CODE</code> in his group. It starts once.\n\n"
            "• The prizes come from <b>your</b> wallet, and you pay the fees.\n"
            "• Share a code only with people you trust: whoever posts it first starts it.\n"
            "• You and the person who posted it can both press its Cancel button.\n"
            "• 📋 <b>My rains &amp; quizzes</b> shows each code and its state, and lets you cancel one that hasn't started.\n"
            "• Codes are forgotten if the bot restarts, so a code that was not used yet stops working."
        )
    if key == "keys":
        return (
            "👛 <b>Wallets</b>\n\n"
            "<b>What it does:</b> lets you back up your wallet, or bring a wallet you already have.\n\n"
            "<b>How to use it:</b> tap 👛 Wallets (or send /keys) in private, then choose:\n"
            "• <b>Export</b>: I show the private key for 2 minutes, then delete the message. Tap 📋 Copy key and keep it somewhere safe. Screenshots and forwarding are blocked.\n"
            "• <b>Import</b>: send the key of a wallet you already have. It becomes the wallet in use, and your other wallet is <b>kept</b>, never deleted.\n"
            "• <b>Switch wallet</b>: choose which of your wallets the bot uses. Find it under <b>👛 Wallets</b>.\n"
            "• <b>Delete a wallet</b>: remove a wallet you no longer want. You must confirm twice. If it is your only wallet of that type, a new empty one is created for you. Export it first if it holds anything.\n\n"
            "⚠️ Anyone with your key controls your funds. Never share it, and never send it in a group."
        )
    if key == "notify":
        return (
            "🔔 <b>Notifications</b>\n\n"
            "<b>What it does:</b> I message you in private whenever something happens in your wallet.\n\n"
            "You get a message for:\n"
            "• a deposit or a transfer you received (with the sender and a link to the transaction)\n"
            "• a transaction that was confirmed or failed\n"
            "• a bridge that arrived\n"
            "• a token you don't have on your list yet: I ask <b>Do you want to add this token?</b>\n"
            "• 🪂 <b>airdrops</b>: someone joined or finished yours, a username needs your review, your airdrop ended or rewards are still to pay, and a prize you won\n\n"
            "To receive them you must have started me once, and not blocked me."
        )
    if key == "assistant":
        return (
            "🤖 <b>AI Assistant</b>\n\n"
            "<b>What it does:</b> talk to it like a person, like Gemini, Grok or ChatGPT. Ask how anything in this bot works, ask for an example, "
            "ask a price, ask about crypto, Neo or the news (it can search the web), or ask it to write, translate, explain or help with code. "
            "For this bot it uses the bot's own guides and live prices, so the steps and button names are the real ones.\n\n"
            "<b>In private:</b>\n"
            "1. Open <b>❓ Help</b> and tap <b>🤖 AI Assistant</b>.\n"
            "2. Type your question, for example <i>How do I tip in a group?</i>\n"
            "3. Follow up in your own words: <i>give me an example</i>, <i>what about the fee?</i>, <i>explain again</i>. "
            "It remembers what you were talking about (for about an hour).\n"
            "4. Tap <b>🧹 New chat</b> (or type <i>new chat</i>) to start a fresh conversation. Tap any menu button when you are done.\n\n"
            "<b>In a group:</b>\n"
            "• Mention me: <code>@NeoRedPillWalletBot how do I tip?</code>\n"
            "• Or reply to one of my messages with your question. To continue, reply to my answer again.\n"
            "• Each person has their own conversation, so nobody mixes up.\n"
            "• <b>It reads the chat:</b> reply to any message in the group and mention me (for example <i>@NeoRedPillWalletBot is this true?</i> or "
            "<i>summarize this</i>). I read that message, the reply thread it belongs to and the latest messages in the group, and answer. "
            "I only keep the last messages in memory, never saved.\n"
            "• <b>Automatic answers:</b> if someone clearly asks about this bot (for example <i>how do I tip in this bot?</i>), I answer on my own, "
            "without a mention. I stay quiet during normal chat, and answer at most a few times per hour so I never spam.\n"
            "• Group admins: <code>/ai off</code> stops automatic answers in that group, <code>/ai on</code> turns them back on, <code>/ai</code> shows the status. "
            "Mentions and replies always work.\n\n"
            "<b>Memes and pictures:</b> ask <i>make a meme about waiting for the bridge</i> or send <code>/meme your idea</code>. "
            "I write the joke, draw the picture and put the text on it. Each person can make a limited number a day.\n\n"
            "<b>Prices:</b> ask <i>100 NRP price</i>, <i>how much is 5 BNB in USD</i> or <i>1000 NRP to GAS</i>. "
            "I work it out from the live prices the bot uses and fix small typos in coin names. "
            "For a price in dollars I also send a 📈 <b>chart</b> of the last days (try <i>NRP chart</i>). "
            "Coins the bot only started recording have a shorter chart that grows over time.\n\n"
            "• English and Bangla both work. With the full AI switched on by the bot's owner it understands free-form questions much better.\n"
            "• In a private chat it can read YOUR OWN balances, votes and GAS, and the current airdrops and events, and answer from them. In a group it never shows balances: ask in private. "
            "It never moves money, never shares anyone else's wallet or balance, and never handles keys. It can discuss markets, but it is not financial advice.\n"
            "• Never type a private key or recovery phrase, even to me.\n"
            "• If it is not sure about a detail of this bot, it says so. Tap <b>🆘 Support</b> to reach a person."
        )
    if key == "support":
        return (
            "🆘 <b>Support</b>\n\n"
            "<b>What it does:</b> connects you with a real person from the bot's team.\n\n"
            "<b>How to use it:</b>\n"
            "1. Open <b>❓ Help</b> (or send /help).\n"
            "2. Tap <b>🆘 Support · @aziz1683</b> or <b>🆘 Support · @btc_mahadi</b>. A chat with that person opens in Telegram.\n"
            "3. Describe the problem: what you tapped, what you expected and what happened. A screenshot helps.\n\n"
            "• The same two people approve airdrop announcements.\n"
            "• Never send your private key or recovery phrase to anyone, including support.\n"
            "• Include the transaction link for a missing deposit or transfer."
        )
    return None
