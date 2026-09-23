"""Immutable oracle for fjord-violet:temporal_history."""
import service


def main():
    assert service.current_policy() == 'strict-fjord-violet', service.current_policy()


if __name__ == "__main__":
    main()
