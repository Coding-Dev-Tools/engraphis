"""Immutable oracle for borealis-violet:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-borealis-violet', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
