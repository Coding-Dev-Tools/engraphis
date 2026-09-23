"""Immutable oracle for fjord-west:corrections."""
import service


def main():
    assert service.current_timeout() == 122, service.current_timeout()


if __name__ == "__main__":
    main()
