"""Immutable oracle for delta-north:temporal_history."""
import service


def main():
    assert service.current_policy() == 'strict-delta-north', service.current_policy()


if __name__ == "__main__":
    main()
