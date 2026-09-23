"""Immutable oracle for juniper-violet:temporal_history."""
import service


def main():
    assert service.current_policy() == 'strict-juniper-violet', service.current_policy()


if __name__ == "__main__":
    main()
