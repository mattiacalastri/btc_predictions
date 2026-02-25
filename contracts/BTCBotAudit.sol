// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title BTCBotAudit
 * @notice On-chain audit trail for BTC Prediction Bot.
 *         Each prediction is committed as a hash before execution and
 *         resolved with the outcome hash once the trade closes.
 *         This provides an immutable, verifiable record of every bet.
 *
 * Deploy on Polygon PoS (chainId 137). Gas per commit+resolve pair: ~80k gas ≈ $0.001.
 */
contract BTCBotAudit {

    // -------------------------------------------------------------------------
    // Ownership
    // -------------------------------------------------------------------------

    address public owner;

    modifier onlyOwner() {
        require(msg.sender == owner, "BTCBotAudit: caller is not the owner");
        _;
    }

    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);

    constructor() {
        owner = msg.sender;
        emit OwnershipTransferred(address(0), msg.sender);
    }

    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "BTCBotAudit: new owner is the zero address");
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }

    // -------------------------------------------------------------------------
    // Storage
    // -------------------------------------------------------------------------

    /// @notice Maps betId to its commit hash (prediction fingerprint at bet time).
    mapping(uint256 => bytes32) public commits;

    /// @notice Maps betId to its resolve hash (outcome fingerprint at trade close).
    mapping(uint256 => bytes32) public resolves;

    /// @notice Maps betId to the trade outcome (true = WIN, false = LOSS).
    mapping(uint256 => bool) public outcomes;

    mapping(uint256 => bool) private _committed;
    mapping(uint256 => bool) private _resolved;

    // -------------------------------------------------------------------------
    // Events
    // -------------------------------------------------------------------------

    event Committed(
        uint256 indexed betId,
        bytes32 commitHash,
        uint256 timestamp
    );

    event Resolved(
        uint256 indexed betId,
        bytes32 resolveHash,
        bool won,
        uint256 timestamp
    );

    // -------------------------------------------------------------------------
    // Write functions
    // -------------------------------------------------------------------------

    /**
     * @notice Commit a prediction hash on-chain at bet time.
     * @param betId      Unique bet identifier (Supabase row id).
     * @param commitHash keccak256(abi.encodePacked(betId, direction, confidence,
     *                   entryPrice, betSize, timestamp)) computed off-chain.
     */
    function commit(uint256 betId, bytes32 commitHash) external onlyOwner {
        require(!_committed[betId], "BTCBotAudit: betId already committed");
        require(commitHash != bytes32(0), "BTCBotAudit: commitHash cannot be zero");

        commits[betId] = commitHash;
        _committed[betId] = true;

        emit Committed(betId, commitHash, block.timestamp);
    }

    /**
     * @notice Resolve an outcome hash on-chain at trade close.
     * @param betId       Unique bet identifier (must have been committed first).
     * @param resolveHash keccak256(abi.encodePacked(betId, exitPrice, pnl,
     *                    won, closeTimestamp)) computed off-chain.
     * @param won         True if the trade resulted in a profit, false otherwise.
     */
    function resolve(uint256 betId, bytes32 resolveHash, bool won) external onlyOwner {
        require(_committed[betId], "BTCBotAudit: betId has not been committed");
        require(!_resolved[betId], "BTCBotAudit: betId already resolved");
        require(resolveHash != bytes32(0), "BTCBotAudit: resolveHash cannot be zero");

        resolves[betId] = resolveHash;
        outcomes[betId] = won;
        _resolved[betId] = true;

        emit Resolved(betId, resolveHash, won, block.timestamp);
    }

    // -------------------------------------------------------------------------
    // View functions
    // -------------------------------------------------------------------------

    function getCommit(uint256 betId) external view returns (bytes32) {
        return commits[betId];
    }

    function getResolve(uint256 betId) external view returns (bytes32 resolveHash, bool won) {
        return (resolves[betId], outcomes[betId]);
    }

    function isCommitted(uint256 betId) external view returns (bool) {
        return _committed[betId];
    }

    function isResolved(uint256 betId) external view returns (bool) {
        return _resolved[betId];
    }
}
