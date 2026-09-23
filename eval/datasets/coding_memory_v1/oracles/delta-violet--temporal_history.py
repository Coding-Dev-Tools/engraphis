"""Immutable oracle for delta-violet:temporal_history."""
import service


def main():
    assert service.current_policy() == 'strict-delta-violet', service.current_policy()


if __name__ == "__main__":
    main()
