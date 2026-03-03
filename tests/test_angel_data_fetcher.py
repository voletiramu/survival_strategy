"""
Unit tests for angel_data_fetcher.py
Tests API response parsing, option chain, Greeks, with mocked HTTP.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch, MagicMock, mock_open
from datetime import datetime, timedelta


# ── Credential Loading Tests ─────────────────────────────────────────────────

class TestLoadCredentials:
    def test_parse_credentials_from_file(self):
        mock_content = (
            "ANGEL_APP_NAME=TestApp\n"
            "ANGEL_APP_TYPE=Historical\n"
            "ANGEL_API_KEY=test_api_key_123\n"
            "ANGEL_CLIENT_CODE=T12345\n"
            "ANGEL_PIN=1234\n"
            "ANGEL_TOTP_KEY=TESTSECRET123\n"
            "BROKER_NAME=Angel One\n"
        )
        with patch('builtins.open', mock_open(read_data=mock_content)):
            from angel_data_fetcher import load_credentials
            creds = load_credentials()
            assert 'Historical' in creds
            assert creds['Historical']['ANGEL_API_KEY'] == 'test_api_key_123'
            assert creds['Historical']['ANGEL_CLIENT_CODE'] == 'T12345'

    def test_handles_inline_comments(self):
        mock_content = (
            "ANGEL_APP_TYPE=Market\n"
            "ANGEL_API_KEY=real_key  # this is a comment\n"
            "ANGEL_CLIENT_CODE=C999\n"
            "ANGEL_PIN=5678\n"
            "ANGEL_TOTP_KEY=SECRET\n"
            "BROKER_NAME=Angel\n"
        )
        with patch('builtins.open', mock_open(read_data=mock_content)):
            from angel_data_fetcher import load_credentials
            creds = load_credentials()
            assert creds['Market']['ANGEL_API_KEY'] == 'real_key'

    def test_handles_empty_lines_and_comments(self):
        mock_content = (
            "# This is a comment\n"
            "\n"
            "ANGEL_APP_TYPE=Test\n"
            "ANGEL_API_KEY=key1\n"
            "\n"
            "# Another comment\n"
            "ANGEL_CLIENT_CODE=C1\n"
            "ANGEL_PIN=1111\n"
            "ANGEL_TOTP_KEY=SEC\n"
            "BROKER_NAME=Angel\n"
        )
        with patch('builtins.open', mock_open(read_data=mock_content)):
            from angel_data_fetcher import load_credentials
            creds = load_credentials()
            assert 'Test' in creds


# ── AngelDataFetcher Tests (with mocked SmartAPI) ────────────────────────────

class TestAngelDataFetcherInit:
    @patch('angel_data_fetcher.load_credentials')
    def test_init_with_valid_app_type(self, mock_creds):
        mock_creds.return_value = {
            'Historical': {
                'ANGEL_API_KEY': 'key', 'ANGEL_CLIENT_CODE': 'C1',
                'ANGEL_PIN': '1234', 'ANGEL_TOTP_KEY': 'secret',
                'ANGEL_APP_NAME': 'Test'
            }
        }
        from angel_data_fetcher import AngelDataFetcher
        with patch('angel_data_fetcher.os.makedirs'):
            fetcher = AngelDataFetcher(app_type='Historical')
            assert fetcher.api_key == 'key'
            assert fetcher.client_code == 'C1'

    @patch('angel_data_fetcher.load_credentials')
    def test_init_falls_back_to_first_available(self, mock_creds):
        mock_creds.return_value = {
            'Market': {
                'ANGEL_API_KEY': 'mkey', 'ANGEL_CLIENT_CODE': 'M1',
                'ANGEL_PIN': '5678', 'ANGEL_TOTP_KEY': 'msec',
                'ANGEL_APP_NAME': 'MktApp'
            }
        }
        from angel_data_fetcher import AngelDataFetcher
        with patch('angel_data_fetcher.os.makedirs'):
            fetcher = AngelDataFetcher(app_type='NonExistent')
            assert fetcher.api_key == 'mkey'


# ── ATM Strike Calculation Tests ─────────────────────────────────────────────

class TestATMStrike:
    @patch('angel_data_fetcher.load_credentials')
    def test_atm_strike_nifty(self, mock_creds):
        mock_creds.return_value = {
            'Historical': {
                'ANGEL_API_KEY': 'k', 'ANGEL_CLIENT_CODE': 'C',
                'ANGEL_PIN': '1', 'ANGEL_TOTP_KEY': 's',
                'ANGEL_APP_NAME': 'T'
            }
        }
        from angel_data_fetcher import AngelDataFetcher
        with patch('angel_data_fetcher.os.makedirs'):
            f = AngelDataFetcher(app_type='Historical')
            assert f.get_atm_strike('NIFTY', spot_price=22345) == 22350
            assert f.get_atm_strike('NIFTY', spot_price=22374) == 22350
            assert f.get_atm_strike('NIFTY', spot_price=22376) == 22400

    @patch('angel_data_fetcher.load_credentials')
    def test_atm_strike_banknifty(self, mock_creds):
        mock_creds.return_value = {
            'Historical': {
                'ANGEL_API_KEY': 'k', 'ANGEL_CLIENT_CODE': 'C',
                'ANGEL_PIN': '1', 'ANGEL_TOTP_KEY': 's',
                'ANGEL_APP_NAME': 'T'
            }
        }
        from angel_data_fetcher import AngelDataFetcher
        with patch('angel_data_fetcher.os.makedirs'):
            f = AngelDataFetcher(app_type='Historical')
            assert f.get_atm_strike('BANKNIFTY', spot_price=48230) == 48200
            assert f.get_atm_strike('BANKNIFTY', spot_price=48260) == 48300

    @patch('angel_data_fetcher.load_credentials')
    def test_atm_strike_none_without_spot(self, mock_creds):
        mock_creds.return_value = {
            'Historical': {
                'ANGEL_API_KEY': 'k', 'ANGEL_CLIENT_CODE': 'C',
                'ANGEL_PIN': '1', 'ANGEL_TOTP_KEY': 's',
                'ANGEL_APP_NAME': 'T'
            }
        }
        from angel_data_fetcher import AngelDataFetcher
        with patch('angel_data_fetcher.os.makedirs'):
            f = AngelDataFetcher(app_type='Historical')
            f.obj = None
            assert f.get_atm_strike('NIFTY') is None


# ── Option Token Filtering Tests ─────────────────────────────────────────────

class TestOptionTokens:
    @patch('angel_data_fetcher.load_credentials')
    def test_get_option_tokens_filters_correctly(self, mock_creds):
        mock_creds.return_value = {
            'Historical': {
                'ANGEL_API_KEY': 'k', 'ANGEL_CLIENT_CODE': 'C',
                'ANGEL_PIN': '1', 'ANGEL_TOTP_KEY': 's',
                'ANGEL_APP_NAME': 'T'
            }
        }
        from angel_data_fetcher import AngelDataFetcher
        with patch('angel_data_fetcher.os.makedirs'):
            f = AngelDataFetcher(app_type='Historical')

        f.instruments_df = pd.DataFrame({
            'name': ['NIFTY']*4 + ['BANKNIFTY']*2,
            'instrumenttype': ['OPTIDX']*6,
            'exch_seg': ['NFO']*4 + ['NFO']*2,
            'symbol': ['NIFTY22000CE', 'NIFTY22000PE', 'NIFTY22050CE', 'NIFTY22050PE',
                       'BANKNIFTY48000CE', 'BANKNIFTY48000PE'],
            'token': ['100', '101', '102', '103', '200', '201'],
            'strike': [22000, 22000, 22050, 22050, 48000, 48000],
            'expiry': pd.to_datetime(['2024-03-28']*6),
        })

        nifty_opts = f.get_option_tokens('NIFTY')
        assert len(nifty_opts) == 4

        strike_opts = f.get_option_tokens('NIFTY', strike=22000)
        assert len(strike_opts) == 2

        ce_opts = f.get_option_tokens('NIFTY', opt_type='CE')
        assert len(ce_opts) == 2
        assert all(s.endswith('CE') for s in ce_opts['symbol'])


# ── Login Test ───────────────────────────────────────────────────────────────

class TestLogin:
    @patch('angel_data_fetcher.load_credentials')
    def test_login_success(self, mock_creds):
        mock_creds.return_value = {
            'Historical': {
                'ANGEL_API_KEY': 'k', 'ANGEL_CLIENT_CODE': 'C',
                'ANGEL_PIN': '1', 'ANGEL_TOTP_KEY': 'JBSWY3DPEHPK3PXP',
                'ANGEL_APP_NAME': 'T'
            }
        }
        from angel_data_fetcher import AngelDataFetcher
        with patch('angel_data_fetcher.os.makedirs'):
            f = AngelDataFetcher(app_type='Historical')

        mock_smart = MagicMock()
        mock_smart.generateSession.return_value = {
            'status': True,
            'data': {'jwtToken': 'jwt123', 'refreshToken': 'ref123'}
        }
        mock_smart.getfeedToken.return_value = 'feed123'

        with patch('angel_data_fetcher.SmartConnect', return_value=mock_smart):
            with patch('angel_data_fetcher.pyotp.TOTP') as mock_totp:
                mock_totp.return_value.now.return_value = '123456'
                result = f.login()
                assert result is True

    @patch('angel_data_fetcher.load_credentials')
    def test_login_failure(self, mock_creds):
        mock_creds.return_value = {
            'Historical': {
                'ANGEL_API_KEY': 'k', 'ANGEL_CLIENT_CODE': 'C',
                'ANGEL_PIN': '1', 'ANGEL_TOTP_KEY': 'JBSWY3DPEHPK3PXP',
                'ANGEL_APP_NAME': 'T'
            }
        }
        from angel_data_fetcher import AngelDataFetcher
        with patch('angel_data_fetcher.os.makedirs'):
            f = AngelDataFetcher(app_type='Historical')

        mock_smart = MagicMock()
        mock_smart.generateSession.return_value = {
            'status': False, 'message': 'Invalid credentials'
        }

        with patch('angel_data_fetcher.SmartConnect', return_value=mock_smart):
            with patch('angel_data_fetcher.pyotp.TOTP') as mock_totp:
                mock_totp.return_value.now.return_value = '123456'
                result = f.login()
                assert result is False


# ── PCR Calculation Test ─────────────────────────────────────────────────────

class TestPCRCalculation:
    def test_pcr_from_chain_data(self):
        chain = pd.DataFrame({
            'type': ['CE', 'CE', 'CE', 'PE', 'PE', 'PE'],
            'oi': [100000, 80000, 60000, 150000, 90000, 70000],
            'strike': [22000, 22050, 22100, 22000, 22050, 22100],
        })
        ce_oi = chain[chain['type'] == 'CE']['oi'].sum()
        pe_oi = chain[chain['type'] == 'PE']['oi'].sum()
        pcr = pe_oi / ce_oi if ce_oi > 0 else 0
        assert ce_oi == 240000
        assert pe_oi == 310000
        assert pcr == pytest.approx(310000 / 240000)
