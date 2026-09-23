"""Immutable oracle for delta-green:temporal_history."""
import service


def main():
    assert service.current_policy() == 'strict-delta-green', service.current_policy()


if __name__ == "__main__":
    main()
