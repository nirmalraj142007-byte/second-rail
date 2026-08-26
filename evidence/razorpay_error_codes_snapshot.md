# Razorpay Error Codes Snapshot

Source: https://razorpay.com/docs/errors/common/ and the per-instrument error tables on https://razorpay.com/docs/payments/payments/test-card-details/ and https://razorpay.com/docs/payments/payments/test-upi-details/. Fetched 2026-08-25. This is a second, independent external label source — reproduced as tables of codes and short descriptions, not the page's prose.

## Common API errors (endpoint-independent)

| code | description | cause |
|---|---|---|
| `BAD_REQUEST_ERROR` | The requested URL was not found on the server | Incorrect API method, or the feature is not enabled on the account. |
| `BAD_REQUEST_ERROR` | Access Denied | Whitelisted IPs configured on the account. |
| `BAD_REQUEST_ERROR` | The api key provided is invalid | Wrong key/secret, or a test-mode key used in live mode (or vice versa). |
| `BAD_REQUEST_ERROR` | The id provided does not exist or access is unauthorised | Entity ID doesn't exist, belongs to a different account, or mode mismatch. |
| `BAD_REQUEST_ERROR` | The amount field is required. | A mandatory request parameter was omitted. |
| `BAD_REQUEST_ERROR` | The amount must be an integer. | Wrong data type or format for a parameter. |
| `BAD_REQUEST_ERROR` | Too many requests | The account's undocumented rate limit was exceeded. |
| `SERVER_ERROR` | We are facing some trouble completing your request at the moment. Please try again shortly. | Transient server-side failure; retry after a short delay. |

## Payment Links — create endpoint errors relevant to this project

| HTTP status | description | cause |
|---|---|---|
| 400 | payment link creation with reference ID already attempted | An existing reference_id has been passed. |
| 400 | amount: cannot be blank. | The request body is missing the amount field, or the body is empty. |
| 400 | amount: amount should be minimum 100 for INR. | The amount is below the per-currency minimum (100 paise / Rs 1.00 for INR). |
| 400 | reference_id: the length must be no more than 40. | reference_id exceeds the 40-character limit. |
| 400 | UPI Payment Links is not supported in Test Mode. Please experience the product in Live Mode. | upi_link=true was passed with test-mode API keys. |

## Card payment failure reasons (test-mode, forced via test card number)

| category | reason | description |
|---|---|---|
| BAD_REQUEST_ERROR | `payment_timed_out` | Your payment could not be completed due to a temporary issue. Try again later. |
| BAD_REQUEST_ERROR | `insufficient_fund` | Your payment could not be completed due to insufficient account balance. Try another card or payment method. |
| BAD_REQUEST_ERROR | `payment_cancelled` | Your payment has been cancelled. Try again or complete the payment later. |
| BAD_REQUEST_ERROR | `card_declined` | Your payment did not go through as it was declined by the bank. Try another payment method or contact your bank. |
| BAD_REQUEST_ERROR | `card_disabled_for_online_payments` | Your card is disabled for online payments. Please reach to your bank or try with another card. |
| BAD_REQUEST_ERROR | `card_number_invalid` | You have entered an incorrect card number. Try again. |
| GATEWAY_ERROR | `gateway_technical_error` | Your payment did not go through due to a temporary issue. Any debited amount will be refunded in 4-5 business days. |
| GATEWAY_ERROR | `authentication_failed` | Your payment could not be completed due to incorrect OTP or verification details. Try another payment method or contact your bank for details. |

## UPI Collect failure reasons (test-mode, forced via amount against failure@razorpay)

| amount (paise) | category | reason | description |
|---|---|---|---|
| 204 | BAD_REQUEST_ERROR | `incorrect_pin` | You have entered an incorrect PIN on the UPI app. Please retry with the correct PIN. |
| 205 | BAD_REQUEST_ERROR | `pin_not_set` | Payment was unsuccessful as you have not set the UPI PIN on the app. Try using another method. |
| 206 | BAD_REQUEST_ERROR | `pin_attempts_exceeded` | Payment was unsuccessful as you have breached the limit to enter UPI PIN incorrectly. Try using another method. |
| 208 | BAD_REQUEST_ERROR | `transaction_limit_exceeded` | Payment was unsuccessful as you exceeded the amount limit for the day with this bank account. Try using another account. |
| 209 | BAD_REQUEST_ERROR | `transaction_limit_exceeded` | Payment was unsuccessful as you exceeded the amount limit for the day with this bank account. Try using another account. |
| 210 | BAD_REQUEST_ERROR | `transaction_frequency_limit_exceeded` | Payment was unsuccessful as you exceeded the number of attempts on the bank account with this UPI ID. Try using another account. |
| 212 | BAD_REQUEST_ERROR | `debit_instrument_blocked` | Payment was unsuccessful as the account linked to this UPI ID is blocked. Try using another account. |
| 304 | BAD_REQUEST_ERROR | `payment_declined` | You have declined the payment request on the UPI app. Please retry when you are ready. |
| 407 | BAD_REQUEST_ERROR | `invalid_device` | Payment was unsuccessful as you may not be registered on the app you are trying to pay with. Try using another method. |
| 104 | GATEWAY_ERROR | `bank_technical_error` | Payment was unsuccessful due to a temporary issue at your bank. Any amount deducted will be refunded within 5-7 working days. |
| 105 | GATEWAY_ERROR | `payment_timed_out` | Payment was unsuccessful due to a temporary issue. Any amount deducted will be refunded within 5-7 working days. |
| 106 | GATEWAY_ERROR | `bank_technical_error` | Payment was unsuccessful due to a temporary issue. Any amount deducted will be refunded within 5-7 working days. |
| 107 | GATEWAY_ERROR | `upi_app_not_available` | Payment was unsuccessful as the UPI app is not reachable at this time. |
| 211 | GATEWAY_ERROR | *(not documented — description only)* | Beneficiary account is blocked. |
| 213 | GATEWAY_ERROR | `beneficiary_account_does_not_exist` | Payment was unsuccessful as the receiver's bank account is inactive. Any amount deducted will be refunded within 5-7 working days. |
| 404 | GATEWAY_ERROR | `payment_risk_check_failed` | Payment was unsuccessful as your account does not pass the risk checks done by your bank. Try using another account. |
| 405 | GATEWAY_ERROR | `payment_risk_check_failed` | Payment was unsuccessful as your account does not pass the risk checks done by your bank. Try using another account. |
| 406 | GATEWAY_ERROR | `duplicate_request` | Payment was unsuccessful due to a temporary issue. If amount got deducted, it will be refunded within 5-7 working days. |

