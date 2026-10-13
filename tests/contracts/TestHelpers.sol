// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

// Helper contracts used ONLY by the automated test suite (not deployed).

interface ITrustPay {
    function refundBuyer(uint256 id) external;
    function markDelivered(uint256 id) external;
}

/// @dev Acts as a malicious seller: when it receives the payout it tries to
///      re-enter TrustPay and trigger a second payout. The attempt is caught so
///      we can assert that the reentrancy guard blocked it.
contract ReentrantSeller {
    ITrustPay public immutable trustPay;
    uint256 public targetId;
    uint256 public receivedCount;
    bytes public lastError;

    constructor(address trustPay_) {
        trustPay = ITrustPay(trustPay_);
    }

    function setTarget(uint256 id) external {
        targetId = id;
    }

    function markDelivered(uint256 id) external {
        trustPay.markDelivered(id);
    }

    receive() external payable {
        receivedCount += 1;
        try trustPay.refundBuyer(targetId) {
            // should never succeed
        } catch (bytes memory reason) {
            lastError = reason;
        }
    }
}

/// @dev A recipient that refuses ETH, used to test the TransferFailed path.
contract RejectingReceiver {
    receive() external payable {
        revert("no thanks");
    }
}
