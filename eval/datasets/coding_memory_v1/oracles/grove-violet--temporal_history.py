"""Immutable oracle for grove-violet:temporal_history."""
import service


def main():
    assert service.current_policy() == 'strict-grove-violet', service.current_policy()


if __name__ == "__main__":
    main()
