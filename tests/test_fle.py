"""Tests for modules/fle/feedback_engine.py"""
import sys; sys.path.insert(0, "..")
from modules.fle.feedback_engine import FeedbackLoopEngine

def test_capture_signal_high_confidence():
    fle = FeedbackLoopEngine()
    sig = fle.capture_signal("user_correction","customer_segments","Enterprise",
                              "6000000","7500000","test",confidence=0.95)
    assert sig.signal_id.startswith("SIG-")
    assert sig.fle_status == "classified"

def test_capture_signal_low_confidence_pending():
    fle = FeedbackLoopEngine()
    sig = fle.capture_signal("user_correction","customer_segments","Enterprise",
                              "6000000","7500000","test",confidence=0.5)
    assert sig.fle_status == "pending"

def test_classify_threshold_error():
    fle = FeedbackLoopEngine()
    err = fle._classify_error("6000000","7500000")
    assert err == "THRESHOLD_ERROR"

def test_classify_retrieval_error():
    fle = FeedbackLoopEngine()
    err = fle._classify_error("Enterprise tier","Enterprise segment")
    assert err == "RETRIEVAL_ERROR"

def test_feedback_summary_keys():
    fle = FeedbackLoopEngine()
    s   = fle.get_feedback_summary()
    assert "total_signals" in s
    assert "applied_count" in s
    assert "correction_loop_closure_rate" in s

def test_learning_velocity_range():
    fle = FeedbackLoopEngine()
    vel = fle.get_learning_velocity(30)
    v   = vel["learning_velocity"]
    assert 0.0 <= v <= 1.0
