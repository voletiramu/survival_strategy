"""
OI HEATMAP v1.0
================
Strike-level OI analysis with directional bias + exit signal for open positions.

build_heatmap()       — per-strike OI, buildup classification, walls
get_directional_bias()— BULLISH / BEARISH / NEUTRAL from heatmap
get_exit_signal()     — 5-factor confidence scoring for profit-taking exits
log_heatmap()         — formatted log output

5-Factor Exit Scoring (0-100):
  1. Wall Hit + OI Drop       (+30 pts max)
  2. Buildup Reversal          (+25 pts max)
  3. Bias Flip                 (+20 pts max)
  4. Max Pain Shift            (+15 pts max)
  5. OI Velocity               (+10-15 pts max)

Decision Rules:
  >= 70  → FULL_EXIT   (close entire position)
  45-69  → TRAIL_50%   (tighten SL to breakeven)
  < 45   → HOLD
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# === Strike intervals per symbol ===
STRIKE_INTERVALS = {
    'NIFTY': 50, 'BANKNIFTY': 100, 'SENSEX': 100,
    'FINNIFTY': 50, 'MIDCPNIFTY': 25,
}

# === Exit scoring thresholds ===
WALL_DISTANCE_PTS = 150        # v24: MC-optimal 100-200 range
WALL_OI_DROP_PCT = 5.0         # Wall OI drop % to confirm unwinding
BUILDUP_FLIP_MIN = 2           # Min strikes flipped for full score
BIAS_FLIP_WEIGHT = 20          # Max pts for opposing bias
MAX_PAIN_SHIFT_PTS = 100       # Min max-pain shift to score
OI_VELOCITY_MIN_PCT = 3.0      # Min 3-min velocity for bonus
EXIT_THRESHOLD_FULL = 65       # v24: MC-optimal 60-70 range (CPR+Wave=60, CPR=60 -> use 65)
EXIT_THRESHOLD_TRAIL = 45      # Confidence >= this → TRAIL_50%


class OIHeatmap:
    """OI heatmap with directional bias and exit signal generation."""

    def __init__(self, dhan_feed=None, zerodha_feed=None):
        """
        Args:
            dhan_feed: DhanFeed instance (primary — has oi_change, Greeks)
            zerodha_feed: ZerodhaFeed instance (fallback)
        """
        self.dhan = dhan_feed
        self.zerodha = zerodha_feed
        self._last_max_pain = {}   # symbol -> float (for shift detection)
        self._last_heatmap = {}    # symbol -> dict (cache)
        self._last_heatmap_time = {}  # symbol -> datetime

    # ------------------------------------------------------------------ #
    #  BUILD HEATMAP                                                      #
    # ------------------------------------------------------------------ #

    def build_heatmap(self, symbol, spot):
        """Build strike-level OI heatmap from live option chain.

        Returns:
            {
                'spot': float,
                'timestamp': datetime,
                'ce_strikes': [{'strike', 'oi', 'oi_change', 'ltp', 'iv', 'buildup'}...],
                'pe_strikes': [{'strike', 'oi', 'oi_change', 'ltp', 'iv', 'buildup'}...],
                'call_walls': [{'strike', 'oi', 'distance_pct'}...],
                'put_walls': [{'strike', 'oi', 'distance_pct'}...],
                'max_pain': float,
                'pcr': float,
                'total_ce_oi': int,
                'total_pe_oi': int,
            }
            or None if no chain data available.
        """
        chain = self._get_chain(symbol)
        if not chain:
            return None

        interval = STRIKE_INTERVALS.get(symbol, 50)
        ce_strikes = []
        pe_strikes = []

        # Parse CE contracts
        for c in chain.get('CE', []):
            strike = c.get('strike', 0)
            oi = c.get('oi', 0) or 0
            oi_prev = c.get('oi_prev', 0) or 0
            oi_change = c.get('oi_change', 0)
            if oi_change == 0 and oi_prev > 0:
                oi_change = oi - oi_prev
            ltp = c.get('ltp', 0) or 0
            iv = c.get('iv', 0) or 0
            volume = c.get('volume', 0) or 0
            prev_volume = c.get('prev_volume', 0) or 0

            buildup = self._classify_buildup(oi, oi_prev, oi_change, ltp,
                                              c.get('prev_close', 0) or 0)
            ce_strikes.append({
                'strike': strike, 'oi': oi, 'oi_change': oi_change,
                'oi_prev': oi_prev, 'ltp': ltp, 'iv': iv,
                'volume': volume, 'buildup': buildup,
            })

        # Parse PE contracts
        for c in chain.get('PE', []):
            strike = c.get('strike', 0)
            oi = c.get('oi', 0) or 0
            oi_prev = c.get('oi_prev', 0) or 0
            oi_change = c.get('oi_change', 0)
            if oi_change == 0 and oi_prev > 0:
                oi_change = oi - oi_prev
            ltp = c.get('ltp', 0) or 0
            iv = c.get('iv', 0) or 0
            volume = c.get('volume', 0) or 0

            buildup = self._classify_buildup(oi, oi_prev, oi_change, ltp,
                                              c.get('prev_close', 0) or 0)
            pe_strikes.append({
                'strike': strike, 'oi': oi, 'oi_change': oi_change,
                'oi_prev': oi_prev, 'ltp': ltp, 'iv': iv,
                'volume': volume, 'buildup': buildup,
            })

        # Sort by strike
        ce_strikes.sort(key=lambda x: x['strike'])
        pe_strikes.sort(key=lambda x: x['strike'])

        # OI walls (top 3 by OI on each side)
        call_walls = sorted([s for s in ce_strikes if s['oi'] > 0],
                            key=lambda x: x['oi'], reverse=True)[:3]
        put_walls = sorted([s for s in pe_strikes if s['oi'] > 0],
                           key=lambda x: x['oi'], reverse=True)[:3]

        # Add distance_pct
        for w in call_walls:
            w['distance_pct'] = abs(w['strike'] - spot) / spot * 100 if spot else 0
        for w in put_walls:
            w['distance_pct'] = abs(w['strike'] - spot) / spot * 100 if spot else 0

        # Totals
        total_ce_oi = sum(s['oi'] for s in ce_strikes)
        total_pe_oi = sum(s['oi'] for s in pe_strikes)
        pcr = total_pe_oi / total_ce_oi if total_ce_oi > 0 else 0

        # Max pain
        max_pain = self._compute_max_pain(ce_strikes, pe_strikes, spot, interval)

        heatmap = {
            'spot': spot,
            'timestamp': datetime.now(),
            'ce_strikes': ce_strikes,
            'pe_strikes': pe_strikes,
            'call_walls': call_walls,
            'put_walls': put_walls,
            'max_pain': max_pain,
            'pcr': round(pcr, 3),
            'total_ce_oi': total_ce_oi,
            'total_pe_oi': total_pe_oi,
        }

        self._last_heatmap[symbol] = heatmap
        self._last_heatmap_time[symbol] = datetime.now()
        return heatmap

    # ------------------------------------------------------------------ #
    #  DIRECTIONAL BIAS                                                   #
    # ------------------------------------------------------------------ #

    def get_directional_bias(self, heatmap):
        """Determine directional bias from OI heatmap.

        Returns: 'BULLISH', 'BEARISH', or 'NEUTRAL'
        """
        if not heatmap:
            return 'NEUTRAL'

        score = 0

        # Factor 1: PCR (PE/CE OI ratio)
        pcr = heatmap.get('pcr', 1.0)
        if pcr > 1.2:
            score += 2   # More PE OI = support = BULLISH
        elif pcr < 0.8:
            score -= 2   # More CE OI = resistance = BEARISH

        # Factor 2: CE wall distance vs PE wall distance
        call_walls = heatmap.get('call_walls', [])
        put_walls = heatmap.get('put_walls', [])
        spot = heatmap.get('spot', 0)

        if call_walls and put_walls and spot > 0:
            nearest_ce_wall = min(w['strike'] for w in call_walls if w['strike'] > spot) if any(w['strike'] > spot for w in call_walls) else spot + 9999
            nearest_pe_wall = max(w['strike'] for w in put_walls if w['strike'] < spot) if any(w['strike'] < spot for w in put_walls) else spot - 9999

            ce_dist = nearest_ce_wall - spot
            pe_dist = spot - nearest_pe_wall

            if pe_dist > 0 and ce_dist > 0:
                # More room upward (CE wall far) = BULLISH
                if ce_dist > pe_dist * 1.5:
                    score += 1
                elif pe_dist > ce_dist * 1.5:
                    score -= 1

        # Factor 3: Buildup patterns
        ce_buildups = [s['buildup'] for s in heatmap.get('ce_strikes', []) if s['oi'] > 0]
        pe_buildups = [s['buildup'] for s in heatmap.get('pe_strikes', []) if s['oi'] > 0]

        # CE SHORT_COVERING or LONG_UNWINDING = resistance weakening = BULLISH
        ce_unwinding = sum(1 for b in ce_buildups if b in ('SHORT_COVERING', 'LONG_UNWINDING'))
        pe_buildup = sum(1 for b in pe_buildups if b in ('LONG_BUILDUP', 'SHORT_BUILDUP'))

        if ce_unwinding >= 2:
            score += 1  # CE resistance weakening
        if pe_buildup >= 2:
            score += 1  # PE support building

        pe_unwinding = sum(1 for b in pe_buildups if b in ('SHORT_COVERING', 'LONG_UNWINDING'))
        ce_buildup = sum(1 for b in ce_buildups if b in ('LONG_BUILDUP', 'SHORT_BUILDUP'))

        if pe_unwinding >= 2:
            score -= 1  # PE support weakening
        if ce_buildup >= 2:
            score -= 1  # CE resistance building

        if score >= 2:
            return 'BULLISH'
        elif score <= -2:
            return 'BEARISH'
        return 'NEUTRAL'

    # ------------------------------------------------------------------ #
    #  EXIT SIGNAL — 5-Factor Scoring                                     #
    # ------------------------------------------------------------------ #

    def get_exit_signal(self, symbol, spot, opt_type, strike, entry_premium,
                        current_premium, oi_velocity_tracker=None):
        """OI-based exit signal for open positions (only for profitable ones).

        Args:
            symbol: 'NIFTY', 'BANKNIFTY', 'SENSEX'
            spot: current spot price
            opt_type: 'CE' or 'PE'
            strike: option strike price
            entry_premium: premium at entry
            current_premium: current premium
            oi_velocity_tracker: OIVelocityTracker instance (optional)

        Returns:
            {
                'should_exit': bool,
                'confidence': 0-100,
                'action': 'FULL_EXIT' | 'TRAIL_50%' | 'HOLD',
                'reason': str,
                'factors': dict
            }
        """
        result = {
            'should_exit': False,
            'confidence': 0,
            'action': 'HOLD',
            'reason': '',
            'factors': {},
        }

        # Safety: only trigger on profitable positions
        if current_premium <= entry_premium:
            result['reason'] = 'not_profitable'
            return result

        try:
            heatmap = self.build_heatmap(symbol, spot)
            if not heatmap:
                result['reason'] = 'no_chain_data'
                return result

            confidence = 0
            factors = {}
            reasons = []

            # --- Factor 1: Wall Hit + OI Drop (0-30 pts) ---
            f1_pts, f1_detail = self._factor_wall_hit(heatmap, spot, opt_type)
            confidence += f1_pts
            factors['wall_hit'] = {'pts': f1_pts, **f1_detail}
            if f1_pts >= 20:
                reasons.append(f"wall_hit({f1_pts})")

            # --- Factor 2: Buildup Reversal (0-25 pts) ---
            f2_pts, f2_detail = self._factor_buildup_reversal(heatmap, opt_type)
            confidence += f2_pts
            factors['buildup_reversal'] = {'pts': f2_pts, **f2_detail}
            if f2_pts >= 15:
                reasons.append(f"buildup_flip({f2_pts})")

            # --- Factor 3: Bias Flip (0-20 pts) ---
            f3_pts, f3_detail = self._factor_bias_flip(heatmap, opt_type)
            confidence += f3_pts
            factors['bias_flip'] = {'pts': f3_pts, **f3_detail}
            if f3_pts >= 15:
                reasons.append(f"bias_flip({f3_pts})")

            # --- Factor 4: Max Pain Shift (0-15 pts) ---
            f4_pts, f4_detail = self._factor_max_pain_shift(symbol, heatmap, strike)
            confidence += f4_pts
            factors['max_pain_shift'] = {'pts': f4_pts, **f4_detail}
            if f4_pts >= 10:
                reasons.append(f"maxpain_shift({f4_pts})")

            # --- Factor 5: OI Velocity (0-15 pts) ---
            f5_pts, f5_detail = self._factor_oi_velocity(
                symbol, strike, opt_type, oi_velocity_tracker)
            confidence += f5_pts
            factors['oi_velocity'] = {'pts': f5_pts, **f5_detail}
            if f5_pts >= 10:
                reasons.append(f"oi_velocity({f5_pts})")

            confidence = int(max(0, min(100, confidence)))
            result['confidence'] = confidence
            result['factors'] = factors

            if confidence >= EXIT_THRESHOLD_FULL:
                result['should_exit'] = True
                result['action'] = 'FULL_EXIT'
                result['reason'] = ' + '.join(reasons) or 'combined_factors'
            elif confidence >= EXIT_THRESHOLD_TRAIL:
                result['should_exit'] = False
                result['action'] = 'TRAIL_50%'
                result['reason'] = ' + '.join(reasons) or 'moderate_signals'
            else:
                result['action'] = 'HOLD'
                result['reason'] = 'insufficient_confidence'

        except Exception as e:
            logger.debug(f"[OIHeatmap] exit_signal failed {symbol}: {e}")
            result['reason'] = f'error: {e}'

        return result

    # ------------------------------------------------------------------ #
    #  FACTOR 1: Wall Hit + OI Drop                                       #
    # ------------------------------------------------------------------ #

    def _factor_wall_hit(self, heatmap, spot, opt_type):
        """Price near OI wall AND that wall's OI is dropping."""
        detail = {'near_wall': False, 'wall_strike': 0, 'wall_oi_drop_pct': 0}

        # CE longs hit resistance at CE walls (above spot)
        # PE longs hit resistance at PE walls (below spot)
        if opt_type == 'CE':
            walls = heatmap.get('call_walls', [])
            # Look for CE walls above spot (resistance)
            relevant = [w for w in walls if w['strike'] >= spot]
        else:
            walls = heatmap.get('put_walls', [])
            # Look for PE walls below spot (support acting as resistance for PE)
            relevant = [w for w in walls if w['strike'] <= spot]

        if not relevant:
            return 0, detail

        # Check nearest wall
        if opt_type == 'CE':
            nearest = min(relevant, key=lambda w: w['strike'] - spot)
            distance = nearest['strike'] - spot
        else:
            nearest = min(relevant, key=lambda w: spot - w['strike'])
            distance = spot - nearest['strike']

        detail['wall_strike'] = nearest['strike']
        detail['distance_pts'] = round(distance, 1)

        if distance > WALL_DISTANCE_PTS * 2:
            return 0, detail  # Too far from wall

        detail['near_wall'] = distance <= WALL_DISTANCE_PTS

        # Check OI drop on that wall
        oi_drop_pct = 0
        oi_change = nearest.get('oi_change', 0)
        oi = nearest.get('oi', 1)
        if oi > 0 and oi_change < 0:
            oi_drop_pct = abs(oi_change) / oi * 100

        detail['wall_oi_drop_pct'] = round(oi_drop_pct, 2)

        pts = 0
        if distance <= WALL_DISTANCE_PTS and oi_drop_pct >= WALL_OI_DROP_PCT:
            pts = 30  # Full score: near wall + significant OI unwinding
        elif distance <= WALL_DISTANCE_PTS * 2 and oi_drop_pct >= WALL_OI_DROP_PCT * 0.6:
            pts = 20  # Moderate: somewhat near + some unwinding
        elif distance <= WALL_DISTANCE_PTS:
            pts = 10  # Near wall but OI not dropping yet

        return pts, detail

    # ------------------------------------------------------------------ #
    #  FACTOR 2: Buildup Reversal                                         #
    # ------------------------------------------------------------------ #

    def _factor_buildup_reversal(self, heatmap, opt_type):
        """Buildup type on strikes near position flips against you."""
        detail = {'flipped_strikes': 0, 'buildups': []}

        if opt_type == 'CE':
            # CE longs threatened by: CE LONG_BUILDUP→LONG_UNWINDING, or SHORT_BUILDUP appearing
            strikes = heatmap.get('ce_strikes', [])
            bearish_buildups = ('LONG_UNWINDING', 'SHORT_BUILDUP')
        else:
            # PE longs threatened by: PE SHORT_BUILDUP→SHORT_COVERING, or LONG_BUILDUP appearing
            strikes = heatmap.get('pe_strikes', [])
            bearish_buildups = ('SHORT_COVERING', 'LONG_BUILDUP')

        flipped = 0
        for s in strikes:
            if s.get('oi', 0) > 0 and s.get('buildup') in bearish_buildups:
                flipped += 1
                detail['buildups'].append(
                    f"{s['strike']}={s['buildup']}")

        detail['flipped_strikes'] = flipped

        if flipped >= BUILDUP_FLIP_MIN:
            return 25, detail
        elif flipped == 1:
            return 15, detail
        return 0, detail

    # ------------------------------------------------------------------ #
    #  FACTOR 3: Bias Flip                                                #
    # ------------------------------------------------------------------ #

    def _factor_bias_flip(self, heatmap, opt_type):
        """Overall directional bias now opposes your position."""
        bias = self.get_directional_bias(heatmap)
        detail = {'bias': bias}

        if opt_type == 'CE' and bias == 'BEARISH':
            return BIAS_FLIP_WEIGHT, detail  # CE long but market BEARISH
        elif opt_type == 'PE' and bias == 'BULLISH':
            return BIAS_FLIP_WEIGHT, detail  # PE long but market BULLISH
        elif bias == 'NEUTRAL':
            return 5, detail  # Neutral = mild concern
        return 0, detail

    # ------------------------------------------------------------------ #
    #  FACTOR 4: Max Pain Shift                                           #
    # ------------------------------------------------------------------ #

    def _factor_max_pain_shift(self, symbol, heatmap, strike):
        """Max pain moved away from your option strike."""
        detail = {'max_pain': 0, 'prev_max_pain': 0, 'shift_pts': 0}

        current_mp = heatmap.get('max_pain', 0)
        detail['max_pain'] = current_mp

        prev_mp = self._last_max_pain.get(symbol, 0)
        detail['prev_max_pain'] = prev_mp

        # Update stored max pain
        if current_mp > 0:
            self._last_max_pain[symbol] = current_mp

        if prev_mp == 0 or current_mp == 0:
            return 0, detail  # No history yet

        shift = abs(current_mp - strike)
        detail['shift_pts'] = round(shift, 1)

        if shift > MAX_PAIN_SHIFT_PTS * 2:
            return 15, detail  # Large shift away
        elif shift > MAX_PAIN_SHIFT_PTS:
            return 10, detail  # Moderate shift
        return 0, detail

    # ------------------------------------------------------------------ #
    #  FACTOR 5: OI Velocity                                              #
    # ------------------------------------------------------------------ #

    def _factor_oi_velocity(self, symbol, strike, opt_type, tracker):
        """3-min OI velocity on your side of the market."""
        detail = {'velocity_pct': 0, 'available': False}

        if not tracker:
            return 0, detail

        try:
            velocity = tracker.get_velocity(
                symbol, strike, opt_type, datetime.now(), window_minutes=3)
            if velocity is None:
                return 0, detail

            detail['available'] = True
            detail['velocity_pct'] = round(velocity * 100, 2)

            # For exit: negative velocity (OI dropping) on your side = bad
            # CE long: CE OI dropping = support eroding
            # PE long: PE OI dropping = support eroding
            if velocity < 0:
                drop_pct = abs(velocity * 100)
                if drop_pct >= WALL_OI_DROP_PCT:
                    return 15, detail
                elif drop_pct >= OI_VELOCITY_MIN_PCT:
                    return 10, detail
        except Exception as e:
            detail['error'] = str(e)

        return 0, detail

    # ------------------------------------------------------------------ #
    #  LOG HEATMAP                                                        #
    # ------------------------------------------------------------------ #

    def log_heatmap(self, symbol, heatmap):
        """Log formatted OI heatmap summary."""
        if not heatmap:
            return

        spot = heatmap['spot']
        bias = self.get_directional_bias(heatmap)

        lines = [f"  OI_HEATMAP: {symbol} spot={spot:.1f} bias={bias} "
                 f"PCR={heatmap['pcr']:.2f} maxpain={heatmap['max_pain']:.0f}"]

        # Top CE walls (resistance)
        for w in heatmap.get('call_walls', [])[:3]:
            chg = w.get('oi_change', 0)
            chg_str = f"+{chg:,.0f}" if chg >= 0 else f"{chg:,.0f}"
            lines.append(f"    CE_WALL: {w['strike']} OI={w['oi']:,.0f} "
                        f"chg={chg_str} dist={w.get('distance_pct', 0):.2f}%")

        # Top PE walls (support)
        for w in heatmap.get('put_walls', [])[:3]:
            chg = w.get('oi_change', 0)
            chg_str = f"+{chg:,.0f}" if chg >= 0 else f"{chg:,.0f}"
            lines.append(f"    PE_WALL: {w['strike']} OI={w['oi']:,.0f} "
                        f"chg={chg_str} dist={w.get('distance_pct', 0):.2f}%")

        # Buildup summary
        ce_buildups = {}
        for s in heatmap.get('ce_strikes', []):
            b = s.get('buildup', 'UNKNOWN')
            ce_buildups[b] = ce_buildups.get(b, 0) + 1
        pe_buildups = {}
        for s in heatmap.get('pe_strikes', []):
            b = s.get('buildup', 'UNKNOWN')
            pe_buildups[b] = pe_buildups.get(b, 0) + 1

        if ce_buildups:
            ce_str = ' '.join(f"{k}={v}" for k, v in ce_buildups.items() if k != 'UNKNOWN')
            if ce_str:
                lines.append(f"    CE_BUILDUP: {ce_str}")
        if pe_buildups:
            pe_str = ' '.join(f"{k}={v}" for k, v in pe_buildups.items() if k != 'UNKNOWN')
            if pe_str:
                lines.append(f"    PE_BUILDUP: {pe_str}")

        for line in lines:
            logger.info(line)

    # ------------------------------------------------------------------ #
    #  INTERNAL HELPERS                                                    #
    # ------------------------------------------------------------------ #

    def _get_chain(self, symbol):
        """Get option chain from Dhan (primary) or Zerodha (fallback)."""
        # Try Dhan first (has oi_change, Greeks natively)
        if self.dhan and hasattr(self.dhan, 'get_option_chain'):
            try:
                chain = self.dhan.get_option_chain(symbol)
                if chain and (chain.get('CE') or chain.get('PE')):
                    return chain
            except Exception:
                pass

        # Fallback to Zerodha
        if self.zerodha and hasattr(self.zerodha, 'get_option_chain'):
            try:
                chain = self.zerodha.get_option_chain(symbol)
                if chain and (chain.get('CE') or chain.get('PE')):
                    return chain
            except Exception:
                pass

        return None

    def _classify_buildup(self, oi, oi_prev, oi_change, ltp, prev_close):
        """Classify OI buildup pattern.

        - LONG_BUILDUP:    OI up + price up   (fresh longs)
        - SHORT_BUILDUP:   OI up + price down (fresh shorts)
        - SHORT_COVERING:  OI down + price up (shorts exiting)
        - LONG_UNWINDING:  OI down + price down (longs exiting)
        """
        if oi_change == 0 and oi_prev > 0:
            oi_change = oi - oi_prev

        oi_up = oi_change > 0
        price_up = ltp > prev_close if prev_close > 0 else None

        if price_up is None:
            return 'UNKNOWN'

        if oi_up and price_up:
            return 'LONG_BUILDUP'
        elif oi_up and not price_up:
            return 'SHORT_BUILDUP'
        elif not oi_up and price_up:
            return 'SHORT_COVERING'
        else:
            return 'LONG_UNWINDING'

    def _compute_max_pain(self, ce_strikes, pe_strikes, spot, interval):
        """Compute max pain strike from CE/PE OI data."""
        if not ce_strikes and not pe_strikes:
            return spot

        all_strikes = set()
        ce_oi = {}
        pe_oi = {}

        for s in ce_strikes:
            k = s['strike']
            all_strikes.add(k)
            ce_oi[k] = s.get('oi', 0)

        for s in pe_strikes:
            k = s['strike']
            all_strikes.add(k)
            pe_oi[k] = s.get('oi', 0)

        if not all_strikes:
            return spot

        min_pain = float('inf')
        max_pain_strike = spot

        for K in sorted(all_strikes):
            pain = 0
            # CE pain: sum of max(0, K - strike) * OI for all CE strikes
            for strike, oi in ce_oi.items():
                intrinsic = max(0, K - strike)
                pain += intrinsic * oi
            # PE pain: sum of max(0, strike - K) * OI for all PE strikes
            for strike, oi in pe_oi.items():
                intrinsic = max(0, strike - K)
                pain += intrinsic * oi

            if pain < min_pain:
                min_pain = pain
                max_pain_strike = K

        return max_pain_strike
