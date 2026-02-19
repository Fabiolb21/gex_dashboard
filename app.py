"""
SPX Gamma Exposure (GEX) Dashboard
Real-time options gamma exposure tracking with WebSocket streaming
Supports multiple underlyings: SPX, NDX, SPY, QQQ, IWM, DIA
"""
import streamlit as st
import json
import time
from datetime import datetime, timedelta
from websocket import create_connection
import pandas as pd
import plotly.graph_objects as go
from utils.auth import ensure_streamer_token
from utils.gex_calculator import GEXCalculator, parse_option_symbol

st.set_page_config(page_title="GEX Dashboard", page_icon="📊", layout="wide")

# Preset symbol configuration
PRESET_SYMBOLS = {
    "SPX": {"option_prefix": "SPXW", "default_price": 6000, "increment": 5},
    "NDX": {"option_prefix": "NDXP", "default_price": 20000, "increment": 25},
    "SPY": {"option_prefix": "SPY", "default_price": 680, "increment": 1},
    "QQQ": {"option_prefix": "QQQ", "default_price": 612, "increment": 1},
    "IWM": {"option_prefix": "IWM", "default_price": 240, "increment": 1},
    "DIA": {"option_prefix": "DIA", "default_price": 450, "increment": 1},
}

DXFEED_URL = "wss://tasty-openapi-ws.dxfeed.com/realtime"


def connect_websocket(token):
    """Connect to dxFeed WebSocket"""
    ws = create_connection(DXFEED_URL, timeout=10)

    # SETUP
    ws.send(json.dumps({
        "type": "SETUP",
        "channel": 0,
        "keepaliveTimeout": 60,
        "acceptKeepaliveTimeout": 60,
        "version": "1.0.0"
    }))
    ws.recv()

    # AUTH
    while True:
        msg = json.loads(ws.recv())
        if msg.get("type") == "AUTH_STATE":
            if msg["state"] == "UNAUTHORIZED":
                ws.send(json.dumps({"type": "AUTH", "channel": 0, "token": token}))
            elif msg["state"] == "AUTHORIZED":
                break

    # FEED channel
    ws.send(json.dumps({
        "type": "CHANNEL_REQUEST",
        "channel": 1,
        "service": "FEED",
        "parameters": {"contract": "AUTO"}
    }))
    msg = json.loads(ws.recv())

    return ws


def get_underlying_price(ws, symbol):
    """Get underlying price - tries Trade first (most accurate), falls back to Quote midpoint"""
    ws.send(json.dumps({
        "type": "FEED_SUBSCRIPTION",
        "channel": 1,
        "add": [
            {"symbol": symbol, "type": "Trade"},
            {"symbol": symbol, "type": "Quote"}
        ]
    }))

    trade_price = None
    quote_mid = None
    start = time.time()

    while time.time() - start < 5:
        try:
            ws.settimeout(1)
            msg = json.loads(ws.recv())
            if msg.get("type") == "FEED_DATA":
                for data in msg.get("data", []):
                    if data.get("eventSymbol") == symbol:
                        event_type = data.get("eventType")

                        # Prefer Trade price (last trade)
                        if event_type == "Trade":
                            price = data.get("price")
                            if price:
                                trade_price = float(price)

                        # Fallback: Quote midpoint
                        elif event_type == "Quote":
                            bid = data.get("bidPrice")
                            ask = data.get("askPrice")
                            if bid and ask:
                                try:
                                    quote_mid = (float(bid) + float(ask)) / 2
                                except (ValueError, TypeError):
                                    pass

            # Return Trade price if we have it, otherwise Quote mid
            if trade_price:
                return trade_price
            elif quote_mid:
                return quote_mid

        except:
            continue

    # Return whichever we got
    return trade_price or quote_mid


def generate_option_symbols(center_price, option_prefix, expiration, strikes_up, strikes_down, increment):
    """Generate option symbols around center price"""
    center_strike = round(center_price / increment) * increment
    strikes = []

    for i in range(-strikes_down, strikes_up + 1):
        strike = center_strike + (i * increment)
        strikes.append(strike)

    options = []
    for strike in strikes:
        # Format strike: use int if whole number, else keep decimal
        if strike == int(strike):
            strike_str = str(int(strike))
        else:
            strike_str = str(strike)

        options.append(f".{option_prefix}{expiration}C{strike_str}")
        options.append(f".{option_prefix}{expiration}P{strike_str}")

    return options


def fetch_option_data(ws, symbols, wait_seconds=15):
    """Fetch Greeks, Summary (OI), and Trade (Volume) for options"""
    subscriptions = []
    for symbol in symbols:
        subscriptions.extend([
            {"symbol": symbol, "type": "Greeks"},
            {"symbol": symbol, "type": "Summary"},
            {"symbol": symbol, "type": "Trade"},
        ])

    ws.send(json.dumps({
        "type": "FEED_SUBSCRIPTION",
        "channel": 1,
        "add": subscriptions
    }))

    data = {}
    start = time.time()

    while time.time() - start < wait_seconds:
        try:
            ws.settimeout(0.5)
            msg = json.loads(ws.recv())

            if msg.get("type") == "FEED_DATA":
                for item in msg.get("data", []):
                    symbol = item.get("eventSymbol")
                    event_type = item.get("eventType")

                    if symbol not in data:
                        data[symbol] = {}

                    if event_type == "Greeks":
                        data[symbol]["gamma"] = item.get("gamma")
                        data[symbol]["delta"] = item.get("delta")
                        data[symbol]["iv"] = item.get("volatility")
                    elif event_type == "Summary":
                        data[symbol]["oi"] = item.get("openInterest")
                    elif event_type == "Trade":
                        # Cumulative volume from Trade events
                        data[symbol]["volume"] = item.get("dayVolume", 0)
        except:
            continue

    return data


def aggregate_by_strike(option_data):
    """Aggregate volume and OI by strike from option data"""
    strike_data = {}

    for symbol, data in option_data.items():
        parsed = parse_option_symbol(symbol)
        if not parsed:
            continue

        strike = parsed['strike']
        opt_type = parsed['type']

        if strike not in strike_data:
            strike_data[strike] = {
                'call_oi': 0,
                'put_oi': 0,
                'call_volume': 0,
                'put_volume': 0,
                'call_iv': None,
                'put_iv': None
            }

        # Convert to numbers (might be strings from WebSocket or NaN)
        import math

        try:
            oi = float(data.get('oi', 0) or 0)
            if math.isnan(oi):
                oi = 0
        except (ValueError, TypeError):
            oi = 0

        try:
            volume = float(data.get('volume', 0) or 0)
            if math.isnan(volume):
                volume = 0
        except (ValueError, TypeError):
            volume = 0

        iv = data.get('iv')

        if opt_type == 'C':
            strike_data[strike]['call_oi'] += oi
            strike_data[strike]['call_volume'] += volume
            if iv is not None and not math.isnan(iv):
                strike_data[strike]['call_iv'] = iv
        else:  # Put
            strike_data[strike]['put_oi'] += oi
            strike_data[strike]['put_volume'] += volume
            if iv is not None and not math.isnan(iv):
                strike_data[strike]['put_iv'] = iv

    # Convert to DataFrame
    df = pd.DataFrame([
        {
            'strike': strike,
            'call_oi': data['call_oi'],
            'put_oi': data['put_oi'],
            'call_volume': data['call_volume'],
            'put_volume': data['put_volume'],
            'call_iv': data['call_iv'],
            'put_iv': data['put_iv'],
            'total_oi': data['call_oi'] + data['put_oi'],
            'total_volume': data['call_volume'] + data['put_volume']
        }
        for strike, data in strike_data.items()
    ]).sort_values('strike').reset_index(drop=True)

    return df


def main():
    st.title("📊 Gamma Exposure (GEX) Dashboard")
    st.caption("Real-time options gamma exposure tracking")

    # Initialize session state
    if 'data_fetched' not in st.session_state:
        st.session_state.data_fetched = False
    if 'gex_calculator' not in st.session_state:
        st.session_state.gex_calculator = None
    if 'underlying_price' not in st.session_state:
        st.session_state.underlying_price = None
    if 'option_data' not in st.session_state:
        st.session_state.option_data = {}
    if 'auto_refresh' not in st.session_state:
        st.session_state.auto_refresh = False
    if 'volume_view' not in st.session_state:
        st.session_state.volume_view = "Calls vs Puts"

    # Sidebar Configuration
    with st.sidebar:
        st.header("⚙️ Configuration")

        # Symbol Selection
        symbol = st.selectbox(
            "Underlying Symbol",
            list(PRESET_SYMBOLS.keys()),
            index=0,
            help="Select the underlying index or ETF to track"
        )

        preset = PRESET_SYMBOLS[symbol]
        option_prefix = preset['option_prefix']
        default_price = preset['default_price']
        increment = preset['increment']

        st.session_state.symbol = symbol

        # Expiration date
        st.subheader("📅 Expiration")
        exp_type = st.radio("Select expiration", ["Today (0DTE)", "Custom Date"], index=0)

        if exp_type == "Today (0DTE)":
            expiration = datetime.now().strftime("%y%m%d")
        else:
            custom_date = st.date_input(
                "Expiration Date",
                value=datetime.now(),
                min_value=datetime.now(),
                max_value=datetime.now() + timedelta(days=365)
            )
            expiration = custom_date.strftime("%y%m%d")

        st.session_state.expiration = expiration

        # Strike range
        st.subheader("🎯 Strike Range")
        strikes_up = st.number_input("Strikes Above", min_value=5, max_value=100, value=25, step=5)
        strikes_down = st.number_input("Strikes Below", min_value=5, max_value=100, value=25, step=5)

        # Fetch interval
        st.subheader("🔄 Data Fetch")
        wait_seconds = st.slider("Fetch Duration (seconds)", min_value=5, max_value=30, value=15, step=5,
                                 help="How long to collect data from WebSocket")

        # Auto-refresh toggle
        auto_refresh = st.checkbox("Auto-refresh", value=st.session_state.auto_refresh,
                                   help="Automatically refetch data every cycle")
        st.session_state.auto_refresh = auto_refresh

        # Fetch Data Button
        if st.button("🔄 Fetch Data", type="primary", use_container_width=True):
            with st.spinner("Connecting to Tastytrade..."):
                try:
                    # Get streamer token
                    token = ensure_streamer_token()

                    # Connect WebSocket
                    ws = connect_websocket(token)

                    # Get underlying price
                    st.info(f"Fetching {symbol} price...")
                    underlying_price = get_underlying_price(ws, symbol)

                    if not underlying_price:
                        st.warning(f"Could not fetch {symbol} price, using default: ${default_price}")
                        underlying_price = default_price

                    st.session_state.underlying_price = underlying_price
                    st.success(f"{symbol} Price: ${underlying_price:,.2f}")

                    # Generate option symbols
                    st.info(f"Generating option symbols around ${underlying_price:,.2f}...")
                    options = generate_option_symbols(
                        underlying_price,
                        option_prefix,
                        expiration,
                        strikes_up,
                        strikes_down,
                        increment
                    )
                    st.info(f"Fetching data for {len(options)} options (duration: {wait_seconds}s)...")

                    # Fetch option data
                    option_data = fetch_option_data(ws, options, wait_seconds)

                    # Close WebSocket
                    ws.close()

                    # Calculate GEX
                    st.info("Calculating gamma exposure...")
                    calc = GEXCalculator(spot_price=underlying_price)

                    for symbol_str, data in option_data.items():
                        gamma = data.get('gamma')
                        oi = data.get('oi')
                        if gamma is not None and oi is not None:
                            calc.update_gamma(symbol_str, gamma, oi)

                    st.session_state.gex_calculator = calc
                    st.session_state.option_data = option_data
                    st.session_state.data_fetched = True

                    st.success("✅ Data fetched successfully!")

                except Exception as e:
                    st.error(f"Error: {e}")
                    st.session_state.data_fetched = False

    # Main Content
    if not st.session_state.data_fetched:
        st.info("👈 Configure settings in the sidebar and click 'Fetch Data' to begin")
        st.markdown("""
        ### Features:
        - 📊 Real-time gamma exposure tracking
        - 📈 GEX by strike visualization
        - 🎯 Zero Gamma (Flip) level calculation
        - 📉 Volume & Open Interest analysis
        - 📊 Implied Volatility skew
        - 🔄 Auto-refresh capability
        
        ### Supported Underlyings:
        - **SPX** - S&P 500 Index
        - **NDX** - Nasdaq 100 Index
        - **SPY** - S&P 500 ETF
        - **QQQ** - Nasdaq 100 ETF
        - **IWM** - Russell 2000 ETF
        - **DIA** - Dow Jones ETF
        """)
        return

    # Display Results
    calc = st.session_state.gex_calculator
    metrics = calc.get_total_gex_metrics()

    # Aggregate strike data (used for PCR and later sections)
    strike_df = aggregate_by_strike(st.session_state.option_data)

    # Calculate global Put/Call Ratios
    total_call_oi = strike_df['call_oi'].sum()
    total_put_oi = strike_df['put_oi'].sum()
    total_call_vol = strike_df['call_volume'].sum()
    total_put_vol = strike_df['put_volume'].sum()

    pcr_oi = total_put_oi / total_call_oi if total_call_oi > 0 else 0
    pcr_vol = total_put_vol / total_call_vol if total_call_vol > 0 else 0

    # Determine PCR sentiment
    def pcr_sentiment(pcr):
        if pcr == 0:
            return "N/A", "gray"
        elif pcr < 0.7:
            return "🟢 Bullish", "normal"
        elif pcr < 1.0:
            return "🟡 Neutral-Bullish", "normal"
        elif pcr < 1.3:
            return "🟠 Neutral-Bearish", "normal"
        else:
            return "🔴 Bearish", "inverse"

    pcr_oi_label, pcr_oi_delta_color = pcr_sentiment(pcr_oi)
    pcr_vol_label, pcr_vol_delta_color = pcr_sentiment(pcr_vol)

    # Header metrics
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    with col1:
        st.metric(f"{st.session_state.symbol} Price", f"${st.session_state.underlying_price:,.2f}")
    with col2:
        st.metric("Options Tracked", f"{metrics['num_options']:,}")
    with col3:
        st.metric("Net GEX", f"${metrics['net_gex']:,.0f}")
    with col4:
        if metrics.get('zero_gamma'):
            st.metric("Zero Gamma", f"${metrics['zero_gamma']:,.2f}")
        else:
            st.metric("Zero Gamma", "N/A")
    with col5:
        st.metric(
            "PCR (OI)",
            f"{pcr_oi:.2f}" if pcr_oi > 0 else "N/A",
            delta=pcr_oi_label,
            delta_color="off",
            help="Put/Call Ratio por Open Interest. <0.7 bullish, 0.7-1.0 neutro-bullish, 1.0-1.3 neutro-bearish, >1.3 bearish"
        )
    with col6:
        st.metric(
            "PCR (Volume)",
            f"{pcr_vol:.2f}" if pcr_vol > 0 else "N/A",
            delta=pcr_vol_label,
            delta_color="off",
            help="Put/Call Ratio por Volume. Indica sentimento de curto prazo. <0.7 bullish, >1.3 bearish"
        )

    st.divider()

    # GEX by Strike
    st.header("🎯 Gamma Exposure by Strike")

    col1, col2 = st.columns([3, 1])

    with col1:
        df = calc.get_gex_by_strike()

        if df.empty:
            st.warning("No GEX data available")
        else:
            # Toggle for chart type
            chart_type = st.radio(
                "Chart Type",
                ["Calls vs Puts", "Net GEX"],
                horizontal=True,
                help="View call/put GEX separately or combined as net GEX"
            )

            fig = go.Figure()

            if chart_type == "Calls vs Puts":
                # Separate bars for calls and puts
                fig.add_trace(go.Bar(
                    x=df['strike'],
                    y=df['call_gex'],
                    name='Call GEX',
                    marker_color='green'
                ))
                fig.add_trace(go.Bar(
                    x=df['strike'],
                    y=-df['put_gex'],  # Negative for visual separation
                    name='Put GEX',
                    marker_color='red'
                ))
                barmode = 'relative'
            else:  # Net GEX
                # Single bar showing net (calls - puts)
                colors = ['green' if x >= 0 else 'red' for x in df['net_gex']]
                fig.add_trace(go.Bar(
                    x=df['strike'],
                    y=df['net_gex'],
                    name='Net GEX',
                    marker_color=colors
                ))
                barmode = 'group'

            # Add vertical line at underlying price
            fig.add_vline(
                x=st.session_state.underlying_price,
                line_dash="dash",
                line_color="orange",
                line_width=2,
                annotation_text=f"${st.session_state.underlying_price:,.2f}",
                annotation_position="top"
            )

            # Add horizontal line at zero for Net GEX
            if chart_type == "Net GEX":
                fig.add_hline(y=0, line_dash="dot", line_color="gray", line_width=1)

            # Add Zero Gamma marker if available
            if metrics.get('zero_gamma'):
                fig.add_vline(
                    x=metrics['zero_gamma'],
                    line_dash="dot",
                    line_color="purple",
                    line_width=2,
                    annotation_text=f"Zero Gamma: ${metrics['zero_gamma']:,.2f}",
                    annotation_position="bottom"
                )

            # Format expiration date for display
            exp_display = st.session_state.expiration
            try:
                exp_date = datetime.strptime(st.session_state.expiration, "%y%m%d")
                exp_display = exp_date.strftime("%b %d, %Y")
            except:
                pass

            fig.update_layout(
                title=f'{st.session_state.symbol} Gamma Exposure - Exp: {exp_display}',
                xaxis_title='Strike Price',
                yaxis_title='Gamma Exposure ($)',
                barmode=barmode,
                template='plotly_white',
                height=500
            )

            st.plotly_chart(fig, use_container_width=True)

    with col2:
        st.subheader("📈 Total GEX")

        st.metric("Total Call GEX", f"${metrics['total_call_gex']:,.0f}")
        st.metric("Total Put GEX", f"${metrics['total_put_gex']:,.0f}")
        st.metric("Net GEX", f"${metrics['net_gex']:,.0f}")

        if metrics['max_gex_strike']:
            st.divider()
            st.metric("Max GEX Strike", f"${metrics['max_gex_strike']:,.0f}")

        if metrics.get('zero_gamma'):
            st.divider()
            zero_gamma = metrics['zero_gamma']
            st.metric(
                "Zero Gamma (Flip)",
                f"${zero_gamma:,.2f}",
                help="Strike where Net GEX crosses zero. Dealers long gamma above, short gamma below."
            )

    # Put/Call Ratio Section
    st.divider()
    st.header("📉 Put/Call Ratio (PCR)")

    pcr_col2, _ = st.columns([1, 2])

    with pcr_col2:
        st.subheader("📊 PCR Global")

        st.metric(
            "PCR (Open Interest)",
            f"{pcr_oi:.2f}" if pcr_oi > 0 else "N/A",
            help="Put OI Total / Call OI Total"
        )
        st.metric(
            "PCR (Volume)",
            f"{pcr_vol:.2f}" if pcr_vol > 0 else "N/A",
            help="Put Volume Total / Call Volume Total"
        )

        st.divider()
        st.markdown("**Interpretação:**")
        st.markdown("""
        - 🟢 **< 0.7** → Bullish (excesso de calls)
        - 🟡 **0.7–1.0** → Neutro-Bullish
        - 🟠 **1.0–1.3** → Neutro-Bearish
        - 🔴 **> 1.3** → Bearish (excesso de puts)
        """)

        st.divider()
        st.markdown("**OI Total:**")
        st.markdown(f"- Calls: `{int(total_call_oi):,}`")
        st.markdown(f"- Puts: `{int(total_put_oi):,}`")

        st.markdown("**Volume Total:**")
        st.markdown(f"- Calls: `{int(total_call_vol):,}`")
        st.markdown(f"- Puts: `{int(total_put_vol):,}`")

    # Volume and Open Interest Section
    # IV Skew Section
    if not strike_df.empty and (strike_df['call_iv'].notna().any() or strike_df['put_iv'].notna().any()):
        st.divider()
        st.header("📈 Implied Volatility Skew")

        fig_iv = go.Figure()

        # Plot Call IV
        call_iv_data = strike_df[strike_df['call_iv'].notna()]
        if not call_iv_data.empty:
            fig_iv.add_trace(go.Scatter(
                x=call_iv_data['strike'],
                y=call_iv_data['call_iv'] * 100,  # Convert to percentage
                mode='lines+markers',
                name='Call IV',
                line=dict(color='green', width=2),
                marker=dict(size=6)
            ))

        # Plot Put IV
        put_iv_data = strike_df[strike_df['put_iv'].notna()]
        if not put_iv_data.empty:
            fig_iv.add_trace(go.Scatter(
                x=put_iv_data['strike'],
                y=put_iv_data['put_iv'] * 100,  # Convert to percentage
                mode='lines+markers',
                name='Put IV',
                line=dict(color='red', width=2),
                marker=dict(size=6)
            ))

        # Add vertical line at underlying price
        fig_iv.add_vline(
            x=st.session_state.underlying_price,
            line_dash="dash",
            line_color="orange",
            line_width=2,
            annotation_text=f"${st.session_state.underlying_price:,.2f}",
            annotation_position="top"
        )

        # Format expiration date for display (YYMMDD -> Mon DD, YYYY)
        exp_display = st.session_state.expiration
        try:
            exp_date = datetime.strptime(st.session_state.expiration, "%y%m%d")
            exp_display = exp_date.strftime("%b %d, %Y")
        except:
            pass

        fig_iv.update_layout(
            title=f'{st.session_state.symbol} Implied Volatility Skew - Exp: {exp_display}',
            xaxis_title='Strike Price',
            yaxis_title='Implied Volatility (%)',
            template='plotly_white',
            height=400,
            hovermode='x unified'
        )

        st.plotly_chart(fig_iv, use_container_width=True)

    st.divider()
    st.header("📊 Volume & Open Interest Analysis")

    if not strike_df.empty:
        # Two columns for OI and Volume charts
        col3, col4 = st.columns(2)

        with col3:
            # Open Interest Chart
            fig_oi = go.Figure()
            fig_oi.add_trace(go.Bar(
                x=strike_df['strike'],
                y=strike_df['call_oi'],
                name='Call OI',
                marker_color='green'
            ))
            fig_oi.add_trace(go.Bar(
                x=strike_df['strike'],
                y=-strike_df['put_oi'],
                name='Put OI',
                marker_color='red'
            ))

            # Add vertical line at underlying price
            fig_oi.add_vline(
                x=st.session_state.underlying_price,
                line_dash="dash",
                line_color="orange",
                line_width=2,
                annotation_text=f"${st.session_state.underlying_price:,.2f}",
                annotation_position="top"
            )

            fig_oi.update_layout(
                title='Open Interest by Strike',
                xaxis_title='Strike',
                yaxis_title='Open Interest',
                barmode='relative',
                template='plotly_white',
                height=400
            )
            st.plotly_chart(fig_oi, use_container_width=True)

        with col4:
            # Volume Chart with toggle
            volume_view = st.radio(
                "Volume View",
                ["Calls vs Puts", "Total Volume"],
                index=["Calls vs Puts", "Total Volume"].index(st.session_state.volume_view),
                key="volume_view_radio",
                horizontal=True,
                help="Switch between separate call/put volume or total volume by strike"
            )
            st.session_state.volume_view = volume_view

            fig_vol = go.Figure()

            if volume_view == "Calls vs Puts":
                # Separate calls and puts
                fig_vol.add_trace(go.Bar(
                    x=strike_df['strike'],
                    y=strike_df['call_volume'],
                    name='Call Volume',
                    marker_color='lightgreen'
                ))
                fig_vol.add_trace(go.Bar(
                    x=strike_df['strike'],
                    y=-strike_df['put_volume'],
                    name='Put Volume',
                    marker_color='lightcoral'
                ))
                barmode = 'relative'
            else:  # Total Volume
                # Total volume (calls + puts)
                total_volume = strike_df['call_volume'] + strike_df['put_volume']
                fig_vol.add_trace(go.Bar(
                    x=strike_df['strike'],
                    y=total_volume,
                    name='Total Volume',
                    marker_color='purple'
                ))
                barmode = 'group'

            # Add vertical line at underlying price
            fig_vol.add_vline(
                x=st.session_state.underlying_price,
                line_dash="dash",
                line_color="orange",
                line_width=2,
                annotation_text=f"${st.session_state.underlying_price:,.2f}",
                annotation_position="top"
            )

            fig_vol.update_layout(
                title=f'Volume by Strike - {volume_view}',
                xaxis_title='Strike',
                yaxis_title='Volume',
                barmode=barmode,
                template='plotly_white',
                height=400
            )
            st.plotly_chart(fig_vol, use_container_width=True)

        # Top Strikes Table
        st.subheader("🔝 Top Strikes")

        # Create tabs for different views
        tab1, tab2, tab3 = st.tabs(["By Total OI", "By Total Volume", "By Put/Call Ratio"])

        with tab1:
            top_oi = strike_df.nlargest(10, 'total_oi')[['strike', 'call_oi', 'put_oi', 'total_oi']]
            top_oi['strike'] = top_oi['strike'].apply(lambda x: f"${x:,.0f}")
            top_oi.columns = ['Strike', 'Call OI', 'Put OI', 'Total OI']
            st.dataframe(top_oi, hide_index=True, use_container_width=True)

        with tab2:
            top_vol = strike_df.nlargest(10, 'total_volume')[['strike', 'call_volume', 'put_volume', 'total_volume']]
            top_vol['strike'] = top_vol['strike'].apply(lambda x: f"${x:,.0f}")
            top_vol.columns = ['Strike', 'Call Vol', 'Put Vol', 'Total Vol']
            st.dataframe(top_vol, hide_index=True, use_container_width=True)

        with tab3:
            # Calculate put/call ratio
            pc_ratio_df = strike_df.copy()
            pc_ratio_df['pc_ratio_oi'] = pc_ratio_df['put_oi'] / pc_ratio_df['call_oi'].replace(0, 1)
            pc_ratio_df['pc_ratio_vol'] = pc_ratio_df['put_volume'] / pc_ratio_df['call_volume'].replace(0, 1)
            top_pc = pc_ratio_df.nlargest(10, 'pc_ratio_oi')[['strike', 'pc_ratio_oi', 'pc_ratio_vol', 'total_oi']]
            top_pc['strike'] = top_pc['strike'].apply(lambda x: f"${x:,.0f}")
            top_pc['pc_ratio_oi'] = top_pc['pc_ratio_oi'].apply(lambda x: f"{x:.2f}")
            top_pc['pc_ratio_vol'] = top_pc['pc_ratio_vol'].apply(lambda x: f"{x:.2f}")
            top_pc.columns = ['Strike', 'P/C Ratio (OI)', 'P/C Ratio (Vol)', 'Total OI']
            st.dataframe(top_pc, hide_index=True, use_container_width=True)

    # Auto-refresh logic
    if st.session_state.auto_refresh:
        time.sleep(1)  # Small delay before rerun
        st.rerun()


if __name__ == "__main__":
    main()
