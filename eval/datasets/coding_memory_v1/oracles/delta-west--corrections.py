"""Immutable oracle for delta-west:corrections."""
import service


def main():
    assert service.current_timeout() == 98, service.current_timeout()


if __name__ == "__main__":
    main()
