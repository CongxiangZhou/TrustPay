// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * @title  TrustPay - Escrow Payment System (v2)
 * @author ZHOU Congxiang (SC6113 Individual Assignment)
 * @notice A buyer locks ETH for a seller. The funds are only paid out when the
 *         buyer confirms the deal, refunded when the seller cancels or the
 *         deadline passes without delivery, or settled by a neutral arbiter if
 *         either party raises a dispute.
 *
 *         v2 adds two timeouts so funds can never be stuck because one party
 *         goes silent:
 *           - Review period: after the seller marks delivery, the buyer has
 *             `reviewPeriod` seconds to release or dispute. If the buyer stays
 *             silent, the seller can claim the payment (`claimPayment`). Once
 *             the review period is over the buyer can no longer dispute, so a
 *             buyer cannot wait it out and then stall the seller with a late
 *             dispute.
 *           - Arbiter window: after a dispute is raised, the arbiter has the same
 *             `reviewPeriod` to decide. If the arbiter never decides, either
 *             party can close the dispute and the buyer is refunded
 *             (`expireDispute`) - the funds go back to whoever paid.
 *
 * @dev    Deployed on the Ethereum Sepolia testnet (chain id 11155111).
 *
 *         Lifecycle:
 *             createEscrow ──► Funded ──markDelivered──► Delivered ──claimPayment──┐
 *                               │  │   (before deadline)  │   │  (review expired) │
 *                 claimRefund ◄─┘  └──── raiseDispute ────┘   │                   │
 *             (after deadline)             │                  │ releaseFunds      │
 *                                          ▼                  ▼                   ▼
 *                                       Disputed ──resolveDispute──▶ Released / Refunded
 *                                          └──expireDispute (arbiter silent) ──▶ Refunded
 *                         refundBuyer (seller cancels, Funded/Delivered) ──▶ Refunded
 *
 *         Security measures:
 *           - Role-based access control (buyer / seller / arbiter) per escrow.
 *           - Explicit state machine: every action checks the current status,
 *             so funds can never be paid out twice.
 *           - Checks-Effects-Interactions + a reentrancy guard on every payout.
 *           - Input validation on all parameters (addresses, amount, deadline,
 *             review period, title length).
 *           - Liveness: timeouts guarantee every escrow can reach a final state
 *             even if the buyer or the arbiter never responds.
 *           - Direct ETH transfers to the contract are rejected (no receive /
 *             fallback), so no ETH can be stuck outside an escrow.
 *
 *         Gas optimisations:
 *           - Custom errors instead of revert strings.
 *           - Struct packing: all fixed-size escrow fields fit into 3 storage
 *             slots by using uint40 timestamps (valid until year 36812):
 *               slot 0: buyer (20) + amount uint96 (12)                 = 32 bytes
 *               slot 1: seller (20) + deadline (5) + status (1)
 *                       + resolvedByArbiter (1) + actionAt (5)          = 32 bytes
 *               slot 2: arbiter (20) + createdAt (5) + reviewPeriod (4) = 29 bytes
 *             so a status change and its timestamp are written in ONE SSTORE.
 *           - `calldata` for string input, `external` functions, constants.
 *           - Full history is emitted as events instead of stored on-chain.
 */
contract TrustPay {
    // ---------------------------------------------------------------------
    // Types
    // ---------------------------------------------------------------------

    enum Status {
        None,       // 0 - escrow does not exist
        Funded,     // 1 - ETH locked, waiting for delivery
        Delivered,  // 2 - seller marked the order as delivered, buyer reviewing
        Disputed,   // 3 - frozen, waiting for the arbiter
        Released,   // 4 - final: ETH paid to the seller
        Refunded    // 5 - final: ETH returned to the buyer
    }

    struct Escrow {
        // slot 0
        address buyer;
        uint96 amount;               // wei; uint96 max ~ 7.9e10 ETH
        // slot 1
        address seller;
        uint40 deadline;             // unix timestamp: seller must mark delivery before this
        Status status;
        bool resolvedByArbiter;
        uint40 actionAt;             // when the current Delivered / Disputed phase started
        // slot 2
        address arbiter;
        uint40 createdAt;
        uint32 reviewPeriod;         // seconds: buyer review window AND arbiter decision window
        // slot 3+
        string title;
    }

    // ---------------------------------------------------------------------
    // Constants
    // ---------------------------------------------------------------------

    uint256 public constant MAX_TITLE_LENGTH = 64;          // bytes
    uint256 public constant MIN_DURATION = 5 minutes;       // short enough to demo a refund
    uint256 public constant MAX_DURATION = 365 days;
    uint256 public constant MIN_REVIEW_PERIOD = 5 minutes;  // short enough to demo the timeouts
    uint256 public constant MAX_REVIEW_PERIOD = 30 days;

    // ---------------------------------------------------------------------
    // Storage
    // ---------------------------------------------------------------------

    uint256 public escrowCount;
    uint256 public totalValueLocked;   // ETH currently held in active escrows
    uint256 public totalReleased;      // lifetime ETH paid to sellers
    uint256 public totalRefunded;      // lifetime ETH returned to buyers

    mapping(uint256 => Escrow) private _escrows;
    mapping(address => uint256[]) private _userEscrows; // ids where user is buyer, seller or arbiter

    uint256 private _lock = 1; // reentrancy guard: 1 = unlocked, 2 = locked

    // ---------------------------------------------------------------------
    // Events
    // ---------------------------------------------------------------------

    event EscrowCreated(
        uint256 indexed id,
        address indexed buyer,
        address indexed seller,
        address arbiter,
        uint256 amount,
        uint64 deadline,
        uint32 reviewPeriod,
        string title
    );
    event DeliveryMarked(uint256 indexed id, address indexed seller);
    event FundsReleased(uint256 indexed id, address indexed seller, uint256 amount);
    event BuyerRefunded(uint256 indexed id, address indexed buyer, uint256 amount);
    event DisputeRaised(uint256 indexed id, address indexed raisedBy);
    event DisputeResolved(uint256 indexed id, address indexed arbiter, bool paidToSeller, uint256 amount);
    event PaymentClaimed(uint256 indexed id, address indexed seller, uint256 amount);
    event DisputeExpired(uint256 indexed id, address indexed buyer, uint256 amount);

    // ---------------------------------------------------------------------
    // Errors
    // ---------------------------------------------------------------------

    error ZeroAmount();
    error AmountTooLarge();
    error InvalidAddress();
    error InvalidParties();
    error InvalidDeadline();
    error InvalidReviewPeriod();
    error InvalidTitle();
    error EscrowNotFound(uint256 id);
    error NotAuthorized();
    error InvalidStatus(Status current);
    error DeadlineNotReached(uint64 deadline);
    error DeadlinePassed(uint64 deadline);
    error WindowStillOpen(uint64 endsAt);
    error ReviewPeriodOver(uint64 endedAt);
    error TransferFailed();
    error ReentrantCall();

    // ---------------------------------------------------------------------
    // Modifiers
    // ---------------------------------------------------------------------

    modifier nonReentrant() {
        if (_lock == 2) revert ReentrantCall();
        _lock = 2;
        _;
        _lock = 1;
    }

    modifier exists(uint256 id) {
        if (_escrows[id].status == Status.None) revert EscrowNotFound(id);
        _;
    }

    // ---------------------------------------------------------------------
    // Write functions
    // ---------------------------------------------------------------------

    /**
     * @notice Create a new escrow and lock `msg.value` wei in it.
     * @param seller       Address that will receive the payment.
     * @param arbiter      Neutral third party who settles disputes.
     * @param deadline     Unix timestamp by which the seller must mark delivery;
     *                     afterwards the buyer may reclaim the funds.
     * @param reviewPeriod Seconds the buyer has to review after delivery, and the
     *                     arbiter has to decide after a dispute (5 min - 30 days).
     * @param title        Short description of the deal (1-64 bytes).
     * @return id          The new escrow id.
     */
    function createEscrow(
        address seller,
        address arbiter,
        uint64 deadline,
        uint32 reviewPeriod,
        string calldata title
    ) external payable returns (uint256 id) {
        if (msg.value == 0) revert ZeroAmount();
        if (msg.value > type(uint96).max) revert AmountTooLarge();
        if (seller == address(0) || arbiter == address(0)) revert InvalidAddress();
        if (seller == msg.sender || arbiter == msg.sender || arbiter == seller) revert InvalidParties();
        if (deadline < block.timestamp + MIN_DURATION || deadline > block.timestamp + MAX_DURATION) {
            revert InvalidDeadline();
        }
        if (reviewPeriod < MIN_REVIEW_PERIOD || reviewPeriod > MAX_REVIEW_PERIOD) revert InvalidReviewPeriod();
        uint256 titleLength = bytes(title).length;
        if (titleLength == 0 || titleLength > MAX_TITLE_LENGTH) revert InvalidTitle();

        id = escrowCount;
        unchecked {
            escrowCount = id + 1; // cannot realistically overflow
        }

        Escrow storage e = _escrows[id];
        e.buyer = msg.sender;
        e.amount = uint96(msg.value);
        e.seller = seller;
        e.deadline = uint40(deadline);          // safe: bounded by MAX_DURATION above
        e.status = Status.Funded;
        e.arbiter = arbiter;
        e.createdAt = uint40(block.timestamp);
        e.reviewPeriod = reviewPeriod;
        e.title = title;

        totalValueLocked += msg.value;

        _userEscrows[msg.sender].push(id);
        _userEscrows[seller].push(id);
        _userEscrows[arbiter].push(id);

        emit EscrowCreated(id, msg.sender, seller, arbiter, msg.value, deadline, reviewPeriod, title);
    }

    /// @notice Seller confirms delivery (must be before the deadline). Starts the buyer's review period.
    function markDelivered(uint256 id) external exists(id) {
        Escrow storage e = _escrows[id];
        if (msg.sender != e.seller) revert NotAuthorized();
        if (e.status != Status.Funded) revert InvalidStatus(e.status);
        if (block.timestamp > e.deadline) revert DeadlinePassed(e.deadline);

        e.status = Status.Delivered;
        e.actionAt = uint40(block.timestamp);   // same storage slot as status: one SSTORE
        emit DeliveryMarked(id, msg.sender);
    }

    /// @notice Buyer is satisfied and releases the payment to the seller.
    function releaseFunds(uint256 id) external exists(id) nonReentrant {
        Escrow storage e = _escrows[id];
        if (msg.sender != e.buyer) revert NotAuthorized();
        if (e.status != Status.Funded && e.status != Status.Delivered) revert InvalidStatus(e.status);

        uint256 amount = e.amount;
        e.status = Status.Released;            // effects before interaction
        totalValueLocked -= amount;
        totalReleased += amount;

        emit FundsReleased(id, e.seller, amount);
        _send(e.seller, amount);
    }

    /// @notice Seller claims the payment when the buyer stayed silent for the whole review period.
    function claimPayment(uint256 id) external exists(id) nonReentrant {
        Escrow storage e = _escrows[id];
        if (msg.sender != e.seller) revert NotAuthorized();
        if (e.status != Status.Delivered) revert InvalidStatus(e.status);
        uint64 endsAt = uint64(e.actionAt) + e.reviewPeriod;
        if (block.timestamp <= endsAt) revert WindowStillOpen(endsAt);

        uint256 amount = e.amount;
        e.status = Status.Released;
        totalValueLocked -= amount;
        totalReleased += amount;

        emit PaymentClaimed(id, e.seller, amount);
        _send(e.seller, amount);
    }

    /// @notice Seller cancels the deal and returns the funds to the buyer.
    function refundBuyer(uint256 id) external exists(id) nonReentrant {
        Escrow storage e = _escrows[id];
        if (msg.sender != e.seller) revert NotAuthorized();
        if (e.status != Status.Funded && e.status != Status.Delivered) revert InvalidStatus(e.status);

        uint256 amount = e.amount;
        e.status = Status.Refunded;
        totalValueLocked -= amount;
        totalRefunded += amount;

        emit BuyerRefunded(id, e.buyer, amount);
        _send(e.buyer, amount);
    }

    /// @notice Buyer reclaims the funds when the deadline passed without delivery.
    function claimRefund(uint256 id) external exists(id) nonReentrant {
        Escrow storage e = _escrows[id];
        if (msg.sender != e.buyer) revert NotAuthorized();
        if (e.status != Status.Funded) revert InvalidStatus(e.status);
        if (block.timestamp <= e.deadline) revert DeadlineNotReached(e.deadline);

        uint256 amount = e.amount;
        e.status = Status.Refunded;
        totalValueLocked -= amount;
        totalRefunded += amount;

        emit BuyerRefunded(id, e.buyer, amount);
        _send(e.buyer, amount);
    }

    /**
     * @notice Buyer or seller freezes the escrow and asks the arbiter to decide. Starts the arbiter window.
     * @dev    After delivery the buyer must dispute within the review period; afterwards the
     *         seller is entitled to claim the payment. The seller may dispute at any time.
     */
    function raiseDispute(uint256 id) external exists(id) {
        Escrow storage e = _escrows[id];
        if (msg.sender != e.buyer && msg.sender != e.seller) revert NotAuthorized();
        if (e.status != Status.Funded && e.status != Status.Delivered) revert InvalidStatus(e.status);
        if (e.status == Status.Delivered && msg.sender == e.buyer) {
            uint64 endsAt = uint64(e.actionAt) + e.reviewPeriod;
            if (block.timestamp > endsAt) revert ReviewPeriodOver(endsAt);
        }

        e.status = Status.Disputed;
        e.actionAt = uint40(block.timestamp);
        emit DisputeRaised(id, msg.sender);
    }

    /**
     * @notice Arbiter settles a dispute. Still allowed after the arbiter window
     *         as long as nobody has called `expireDispute` yet.
     * @param payToSeller true = pay the seller, false = refund the buyer.
     */
    function resolveDispute(uint256 id, bool payToSeller) external exists(id) nonReentrant {
        Escrow storage e = _escrows[id];
        if (msg.sender != e.arbiter) revert NotAuthorized();
        if (e.status != Status.Disputed) revert InvalidStatus(e.status);

        uint256 amount = e.amount;
        e.resolvedByArbiter = true;
        totalValueLocked -= amount;

        address recipient;
        if (payToSeller) {
            e.status = Status.Released;
            totalReleased += amount;
            recipient = e.seller;
        } else {
            e.status = Status.Refunded;
            totalRefunded += amount;
            recipient = e.buyer;
        }

        emit DisputeResolved(id, msg.sender, payToSeller, amount);
        _send(recipient, amount);
    }

    /// @notice If the arbiter did not decide within the window, buyer or seller can close the
    ///         dispute; the funds return to the buyer (the party who paid).
    function expireDispute(uint256 id) external exists(id) nonReentrant {
        Escrow storage e = _escrows[id];
        if (msg.sender != e.buyer && msg.sender != e.seller) revert NotAuthorized();
        if (e.status != Status.Disputed) revert InvalidStatus(e.status);
        uint64 endsAt = uint64(e.actionAt) + e.reviewPeriod;
        if (block.timestamp <= endsAt) revert WindowStillOpen(endsAt);

        uint256 amount = e.amount;
        e.status = Status.Refunded;
        totalValueLocked -= amount;
        totalRefunded += amount;

        emit DisputeExpired(id, e.buyer, amount);
        _send(e.buyer, amount);
    }

    // ---------------------------------------------------------------------
    // View functions
    // ---------------------------------------------------------------------

    /// @notice Full details of one escrow.
    function getEscrow(uint256 id) external view exists(id) returns (Escrow memory) {
        return _escrows[id];
    }

    /// @notice Ids of all escrows where `user` is the buyer, seller or arbiter.
    function getUserEscrows(address user) external view returns (uint256[] memory) {
        return _userEscrows[user];
    }

    /// @notice Platform statistics for the dashboard.
    function getStats()
        external
        view
        returns (uint256 count, uint256 valueLocked, uint256 released, uint256 refunded)
    {
        return (escrowCount, totalValueLocked, totalReleased, totalRefunded);
    }

    // ---------------------------------------------------------------------
    // Internal
    // ---------------------------------------------------------------------

    function _send(address to, uint256 amount) private {
        (bool ok, ) = payable(to).call{value: amount}("");
        if (!ok) revert TransferFailed();
    }
}
